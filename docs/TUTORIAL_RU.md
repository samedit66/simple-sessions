# Серверные сессии с FastAPI

[Краткое руководство](../README.md) · [English](TUTORIAL_EN.md)

Приложение проверило пароль, passkey или ответ провайдера и получило ID пользователя. Как узнать его при следующем запросе? Клиенту выдаём случайный токен, а ID и срок действия оставляем на сервере. Тогда можно завершить сессию одного устройства, пока остальные остаются в аккаунте.

Начнём с хранилища. Первые примеры можно запустить без веб-сервера. Для примеров с HTTP создайте проект на Python 3.12 или новее.

```sh
uv add simple-sessions
```

Если используете pip, выполните `python -m pip install simple-sessions`.

## SessionStore — где живёт сессия

Хранилище получает SHA-256-хеш токена в виде строки из строчных шестнадцатеричных цифр и объект `Session` с полями `user_id`, `created_at` и `expires_at`. Чтобы подключить свою базу, достаточно класса с тремя асинхронными методами. Наследование от протокола необязательно.

| Метод | Что гарантирует |
| --- | --- |
| `create(digest, session, *, replace_digest=None)` | Атомарно сохраняет сессию и удаляет предыдущую, если она есть. Возвращает `True` при успехе. Если хеш уже занят действующей сессией, возвращает `False` и сохраняет обе исходные записи. Место истёкшей сессии можно занять снова. |
| `get(digest)` | Возвращает действующую сессию или `None`, если запись отсутствует или истекла. Срок проверяется и до плановой очистки. |
| `revoke(digest)` | Удаляет запись, если она есть. Вызов можно повторить. |

Если новый и предыдущий хеш совпадают, поднимите `ValueError`. Если к моменту вставки новая сессия уже истекла, поднимите `SessionStoreError`. Для таймаута или временной недоступности есть подкласс `SessionStoreUnavailable`. При таймауте запись уже могла попасть в базу. [Контракт хранилища](../src/simple_sessions/store.py)

### MemorySessionStore для одного процесса

`Sessions()` создаёт собственное хранилище в памяти. Создайте его явно, если хотите очищать истёкшие записи. Метод `cleanup()` запускает очистку один раз и возвращает число удалённых записей. Контекстный менеджер ждёт один интервал до первой очистки и останавливает задачу при выходе.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from simple_sessions import MemorySessionStore, Sessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = MemorySessionStore[int]()
    app.state.sessions = Sessions(store)
    async with store.periodic_cleanup(interval=300):
        yield


app = FastAPI(lifespan=lifespan)
```

### Сохраним сессии в файле SQLite

Сохраните адаптер в `sqlite_store.py`. Для него достаточно стандартной библиотеки Python. В этом примере у пользователей целочисленные ID. Каждая операция открывает своё соединение в рабочем потоке. При записи сначала берём блокировку базы, затем проверяем время и меняем строки. Контекст соединения фиксирует замену сессии целиком или откатывает её.

Метки времени хранятся в UTC в формате фиксированной длины, поэтому их можно сравнивать как строки. Разместите файл на постоянном локальном диске и передайте всем воркерам один путь.

```python
import asyncio
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime

from simple_sessions import Session, SessionStoreError, SessionStoreUnavailable


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class SQLiteSessionStore:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def _connection(self):
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                yield db
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 255 in (
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            ):
                raise SessionStoreUnavailable("Session database is busy") from exc
            raise SessionStoreError("Session database operation failed") from exc

    async def initialize(self):
        await asyncio.to_thread(self._initialize)

    def _initialize(self):
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            )""")

    async def create(self, digest: str, session: Session[int], *,
                     replace_digest: str | None = None) -> bool:
        if digest == replace_digest:
            raise ValueError("New and predecessor digests must differ")
        return await asyncio.to_thread(self._create, digest, session, replace_digest)

    def _create(self, digest, session, predecessor):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = stamp(datetime.now(UTC))
            expires = stamp(session.expires_at)
            if expires <= now:
                raise SessionStoreError("Session expired before insertion")
            result = db.execute("""
                INSERT INTO sessions (digest, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(digest) DO UPDATE SET
                    user_id = excluded.user_id, created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                WHERE sessions.expires_at <= ?
            """, (digest, str(session.user_id), stamp(session.created_at), expires, now))
            if result.rowcount == 0:
                return False
            if predecessor is not None:
                db.execute("DELETE FROM sessions WHERE digest = ?", (predecessor,))
            return True

    async def get(self, digest: str) -> Session[int] | None:
        return await asyncio.to_thread(self._get, digest)

    def _get(self, digest):
        with self._connection() as db:
            row = db.execute(
                "SELECT user_id, created_at, expires_at FROM sessions WHERE digest = ?",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        try:
            expires = datetime.fromisoformat(row[2])
            if expires <= datetime.now(UTC):
                return None
            return Session[int](user_id=int(row[0]), created_at=datetime.fromisoformat(row[1]),
                                expires_at=expires)
        except (ValueError, TypeError) as exc:
            raise SessionStoreError("Invalid stored session") from exc

    async def revoke(self, digest: str) -> None:
        await asyncio.to_thread(self._revoke, digest)

    def _revoke(self, digest):
        with self._connection() as db:
            db.execute("DELETE FROM sessions WHERE digest = ?", (digest,))
```

Проверьте, что новый менеджер видит старую сессию. Сохраните код рядом с адаптером в `demo.py` и выполните `uv run python demo.py`. Для очистки файла можно периодически удалять строки по условию `expires_at <= ?`, передавая время в том же формате UTC.

```python
import asyncio

from simple_sessions import Sessions
from sqlite_store import SQLiteSessionStore


async def main():
    store = SQLiteSessionStore("sessions.db")
    await store.initialize()
    issued = await Sessions[int](store).issue(42)
    reopened = Sessions[int](SQLiteSessionStore("sessions.db"))
    assert await reopened.get_session(issued.token) == issued.session
    await reopened.revoke(issued.token)


asyncio.run(main())
```

### RedisSessionStore для нескольких серверов

Для Redis 7+ установите дополнение командой `uv add 'simple-sessions[redis]'` (через pip — `python -m pip install 'simple-sessions[redis]'`). В `REDIS_URL` укажите адрес основного узла. Воркеры одного окружения используют общее пространство ключей. Redis сам удаляет истёкшие записи. Функции кодирования и декодирования сохраняют тип ID пользователя, здесь это UUID. Приложение владеет клиентом и закрывает его при остановке.

```python
import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from simple_sessions import Sessions
from simple_sessions.redis import RedisSessionStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with Redis.from_url(
        os.environ["REDIS_URL"],
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    ) as client:
        store = RedisSessionStore(
            client,
            namespace="my-app:prod",
            encode_user_id=str,
            decode_user_id=UUID,
        )
        app.state.sessions = Sessions(store)
        yield


app = FastAPI(lifespan=lifespan)
```

## Sessions — выдача, чтение, замена и отзыв токена

Передайте подходящее хранилище в `Sessions(store, ttl=...)`. Каждый вход получает свой токен. По умолчанию сессия живёт час. Срок задаётся целым положительным числом секунд для менеджера или для конкретной выдачи. Чтение сохраняет исходный срок действия.

```python
import asyncio
from simple_sessions import IssuedSession, Session, Sessions


async def main():
    sessions = Sessions[int](ttl=900)
    first: IssuedSession[int] = await sessions.issue(42)
    active: Session[int] | None = await sessions.get_session(first.token)
    assert active == first.session
    second = await sessions.issue(42, ttl=60, replace_token=first.token)
    assert not await sessions.is_valid(first.token)
    assert await sessions.is_valid(second.token)
    await sessions.revoke(second.token)
    assert await sessions.get_session(second.token) is None


asyncio.run(main())
```

`Session` и `IssuedSession` — неизменяемые модели. У выданной сессии поле `.token` содержит токен для передачи клиенту. В repr и при выгрузке модели это поле опускается. Не записывайте токен в URL и логи. Для ID подойдут `int`, `str` или `UUID`. При некорректном, неизвестном или истёкшем токене поиск вернёт `None`. При попытке заменить токен неверного формата `issue` поднимет `ValueError`. Предыдущая запись с корректным токеном к этому моменту уже может отсутствовать.

## CookieAuth — вход и выход в браузере

Сохраните пример в `app.py`. Для демонстрации возьмём одну учётную запись из переменных окружения. В своём приложении подставьте проверку учётных данных из вашей базы. Получив подтверждённый ID, вызовите `login`: он выдаст сессию, заменит предыдущую сессию браузера и установит cookie. При выходе `logout` отзовёт сессию и удалит cookie. В обоих ответах будет `Cache-Control: no-store`.

```python
import os
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from simple_sessions import CookieAuth

app = FastAPI()
auth = CookieAuth[int]()


class Credentials(BaseModel):
    login: str
    password: str


def verified_id(credentials: Credentials) -> int:
    login_ok = compare_digest(credentials.login.encode(), os.environ["ADMIN_LOGIN"].encode())
    password_ok = compare_digest(credentials.password.encode(), os.environ["ADMIN_PASSWORD"].encode())
    if not (login_ok and password_ok):
        raise HTTPException(401)
    return 1


@app.post("/login", status_code=204)
async def login(credentials: Credentials, request: Request, response: Response):
    await auth.login(request, response, verified_id(credentials))


@app.get("/protected")
async def protected(user_id: Annotated[int, Depends(auth.current_user)]):
    return {"user_id": user_id}


@app.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    await auth.logout(request, response)
```

Запустите приложение и выполните четыре команды curl из [README](../README.md#cookie-based-authentication). Если используете pip, установите `uvicorn` и уберите из команды `uv run --with uvicorn`.

```sh
ADMIN_LOGIN=alice ADMIN_PASSWORD=local-demo uv run --with uvicorn uvicorn app:app
```

### CookieSettings и Origin

По умолчанию cookie называется `__Host-session_id`, её атрибуты — `Secure`, `HttpOnly`, `SameSite=lax` и `Path=/`. Для входа и выхода нужен POST с разрешённым Origin. При других запросах на запись с cookie библиотека тоже проверяет Origin. По умолчанию сверяются схема, хост и порт. За прокси настройте получение публичной схемы от доверенного прокси и проверку Host.

Для отдельного фронтенда замените объявление менеджера в примере. Явный список заменяет проверку по умолчанию, поэтому включите в него все источники, которым разрешена запись. Для браузерных запросов между разными origin отдельно настройте CORS с передачей учётных данных.

```python
from simple_sessions import CookieSettings

settings = CookieSettings(
    name="__Host-shop_session",
    same_site="lax",
    allowed_origins=["https://shop.example", "https://account.example"],
)
auth = CookieAuth[int](cookie=settings)
```

Параметр `same_site` принимает `"lax"`, `"strict"` и `"none"`. Для последнего нужен Secure. Для локального HTTP подойдёт `CookieSettings(name="dev_session", secure=False)`. При входе с дубликатами cookie сессии вернётся 400, при запрещённом Origin — 403, при вызове входа или выхода неверным HTTP-методом — 405.

### Защищённый маршрут, гости и данные сессии

Оба транспорта поддерживают следующие методы. Выберите нужный результат для вашего обработчика. Обязательные методы вернут HTTP 401, если учётные данные отсутствуют или недействительны. Необязательные вернут для гостя `None`. В обоих случаях временная недоступность хранилища даст HTTP 503, а проверка Origin для cookie может вернуть 403.

| Результат | Вход обязателен | Можно гостям |
| --- | --- | --- |
| `user_id` | `await auth.require_user(request)` | `await auth.resolve_user(request)` |
| `Session` | `await auth.require_session(request)` | `await auth.resolve_session(request)` |

```python
@app.get("/feed")
async def feed(request: Request):
    user_id = await auth.resolve_user(request)
    return {"feed": "public" if user_id is None else "personal"}


@app.get("/session")
async def session_details(request: Request):
    session = await auth.require_session(request)
    return {"user_id": session.user_id, "expires_at": session.expires_at}
```

### Подключим общий роутер

`Depends(auth.current_user)` привязывает конкретный экземпляр и описывает cookie в OpenAPI. Если один роутер обслуживает несколько приложений, `user_dependency(get_auth)` позволяет получать менеджер для текущего запроса. Добавьте код к примеру с cookie. При нестандартном имени cookie передайте это имя в фабрику как `cookie_name`, чтобы оно попало в схему.

```python
from fastapi import APIRouter

router = APIRouter()
app.state.auth = auth


def get_auth(request: Request) -> CookieAuth[int]:
    return request.app.state.auth


type CurrentUser = Annotated[int, Depends(CookieAuth.user_dependency(get_auth))]


@router.get("/account")
async def account(user_id: CurrentUser):
    return {"user_id": user_id}


app.include_router(router)
```

## BearerAuth — выдадим токен клиенту API

Добавьте маршруты в тот же `app.py`. Они используют ту же проверку учётных данных и отдельный менеджер для API. Чтобы подключить SQLite или Redis, передайте выбранному транспорту `Sessions(store)` с подготовленным хранилищем. Менеджеры с общим хранилищем и пространством ключей видят одни и те же токены.

```python
from simple_sessions import BearerAuth

bearer = BearerAuth[int]()


@app.post("/token")
async def token(credentials: Credentials, response: Response):
    issued = await bearer.sessions.issue(verified_id(credentials), ttl=900)
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": issued.token, "token_type": "bearer"}


@app.get("/api/me")
async def me(user_id: Annotated[int, Depends(bearer.current_user)]):
    return {"user_id": user_id}


@app.post("/api/logout", status_code=204)
async def api_logout(request: Request):
    await bearer.logout(request)
```

Перезапустите приложение после добавления маршрутов. Получите токен, прочитайте данные своего аккаунта и завершите сессию. Последний запрос вернёт 401 с заголовком `WWW-Authenticate: Bearer`. В общем роутере можно указать `Depends(BearerAuth.user_dependency(get_bearer))`, где `get_bearer` возвращает bearer-менеджер этого приложения.

```sh
url=http://localhost:8000
token=$(curl -fsS -H 'Content-Type: application/json' \
  -d '{"login":"alice","password":"local-demo"}' "$url/token" | \
  python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
curl -i -H "Authorization: Bearer $token" "$url/api/me"
curl -i -X POST -H "Authorization: Bearer $token" "$url/api/logout"
curl -i -H "Authorization: Bearer $token" "$url/api/me"
```
