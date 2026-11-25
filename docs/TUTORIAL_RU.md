# Первая сессия

[Пример приложения](../README.md) · [English](TUTORIAL%20EN.md)

## Как магазин узнаёт меня при следующем запросе?

Откройте свои заказы в новом браузере. Магазин попросит войти. Когда он проверяет, кто вы, это **аутентификация**. А когда решает, можно ли вам показать заказ № 123, это **авторизация**. После входа магазин знает ваш ID, но в обработчике заказов всё равно нужно проверить, кому принадлежит заказ.

Личность можно подтвердить по-разному. При входе по паролю приложение сверяет его с сохранённым хешем. При входе с passkey проверяет подпись одноразового запроса. При входе через корпоративный аккаунт по OpenID Connect пользователя узнаёт провайдер. В каждом случае у приложения в итоге есть ID пользователя. Осталось запомнить вход.

Можно отправлять логин и пароль с каждым запросом через HTTP Basic. В примере из README они нужны только при входе. Другой вариант — подписанный токен, например JWT. Сервис, у которого есть ключ проверки, может проверить данные в токене локально. Чтобы досрочно отозвать один такой токен, сервисам придётся дополнительно обмениваться информацией об отзыве или где-то её хранить.

## Попробуем без веб-сервера

При входе сервер выдаёт клиенту случайную строку — **идентификатор сессии**, или session ID. В коде библиотеки она называется `token`. ID аккаунта и срок действия остаются на сервере. Для примера достаточно Python 3.12+ и пакета `simple-sessions`.

```python
import asyncio

from simple_sessions import Sessions


async def main() -> None:
    sessions = Sessions[int](ttl=900)
    issued = await sessions.issue(user_id=42)
    active = await sessions.get_session(issued.token)
    assert active is not None and active.user_id == 42

    await sessions.revoke(issued.token)
    assert await sessions.get_session(issued.token) is None


asyncio.run(main())
```

Число 42 здесь обозначает аккаунт. Если войти под ним дважды, получатся два разных токена. Хранилище получает SHA-256-хеш каждого токена. Сама строка даёт доступ к сессии любому, у кого есть её копия, пока токен действует. Поэтому в URL и логи её не кладут.

Попробуйте убрать строку с `revoke`. Последняя проверка упадёт: сессия всё ещё действует. Здесь виден и плюс, и минус подхода. На каждый запрос приходится читать хранилище, так что от него зависит доступность приложения. Зато достаточно удалить одну запись, и со следующего запроса устройство окажется разлогинено. Уже начатый запрос может завершиться.

## Перенесём сессию в браузер

Запустите три файла из [README](../README.md) и выполните четыре команды curl. При входе приложение проверяет пароль и вызывает `auth.login(request, response, user_id)`. Найдите в ответе заголовок `Set-Cookie`. Браузер сохраняет ID сессии и при запросе к `/me` отправляет его обратно. При выходе сервер отзывает сессию. Последняя команда curl специально отправляет старую cookie ещё раз. В ответ придёт 401.

По умолчанию у cookie есть атрибуты HttpOnly и Secure. Сессия живёт час, чтение срок не продлевает. Для входа и выхода через cookie нужен POST с разрешённым Origin. По умолчанию должны совпасть схема, хост и порт запроса. Если перед приложением стоит прокси, проверяйте допустимый Host и настройте передачу публичной схемы от доверенного прокси.

Если фронтенд живёт на другом origin, передайте менеджеру `cookie=CookieSettings(allowed_origins=["https://app.example.com"])`. Список целиком заменяет проверку по умолчанию. Для запросов браузера между разными origin отдельно настройте CORS с передачей учётных данных.

## Пустим на страницу гостя

В личном кабинете пригодится `require_user`: без действительной сессии вернётся 401. А у ленты может быть и публичная версия. Добавьте такой маршрут в `users.py` из README.

```python
@router.get("/feed")
async def feed(request: Request, auth: Auth):
    user_id = await auth.resolve_user(request)
    return {"feed": "public" if user_id is None else "personal"}
```

Если нужны ещё и даты создания и окончания сессии, вызовите `require_session` или `resolve_session`. В результате уже есть `user_id`, второго чтения не потребуется. Даже маршрут для гостей вернёт 403 при запрещённом origin записи через cookie и 503 при недоступном хранилище.

## Выдадим токен консольному клиенту

Консольный клиент может передавать токен в заголовке `Authorization: Bearer <token>`. Создайте `app.state.auth = BearerAuth[int]()` и используйте `Depends(bearer_user(get_auth))` на защищённых маршрутах. После проверки пользователя выпустите токен через тот же менеджер, который обслуживает маршруты:

```python
issued = await auth.sessions.issue(user_id, ttl=900)
response.headers["Cache-Control"] = "no-store"
return {"access_token": issued.token, "token_type": "bearer"}
```

Чтобы заменить токен одного устройства, передайте в `issue` параметр `replace_token=old_token`. Запись новой сессии и отзыв старой происходят атомарно. При входе через cookie библиотека сама заменяет предыдущую сессию этого браузера. Остальные устройства остаются в аккаунте. Для простой проверки токена есть `is_valid(token)`, а если нужен ещё и ID пользователя — `get_session(token)`.

## Очистка памяти

Если пользователь закрыл браузер, к его сессии могут больше ни разу не обратиться. Срок действия всё равно истечёт вовремя. Чтобы освободить память и под такими записями, включите периодическую очистку на время работы приложения. В этом примере хранилище проверяется каждые пять минут. При остановке приложения контекст дожидается завершения задачи.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from simple_sessions import CookieAuth, MemorySessionStore, Sessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = MemorySessionStore[int]()
    app.state.auth = CookieAuth(Sessions(store))
    async with store.periodic_cleanup(interval=300):
        yield


app = FastAPI(lifespan=lifespan)
```

## Запустим второй воркер

По умолчанию сессии живут в памяти одного процесса. Перезапустили его — пользователям придётся войти заново. А если запустить два воркера с отдельными хранилищами, можно войти через первый и получить 401 от второго. Чтобы они видели одни и те же сессии, установите `simple-sessions[redis]` и подключите оба воркера к одному основному узлу Redis 7+. В этом примере у аккаунтов ID типа UUID:

```python
import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from simple_sessions import CookieAuth, CookieSettings, Sessions
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
        app.state.auth = CookieAuth(
            Sessions(store),
            cookie=CookieSettings(allowed_origins=["https://app.example"]),
        )
        yield


app = FastAPI(lifespan=lifespan)
```

Воркеры одного окружения используют общее пространство ключей. Для тестов и рабочего приложения задайте разные имена. Приложение само открывает и закрывает Redis-клиент. Если время ожидания записи вышло, Redis уже мог её выполнить. Поэтому в примере отключены автоматические повторы. Отзыв одного и того же токена можно повторить безопасно.

Для своей базы реализуйте три метода [SessionStore](../src/simple_sessions/store.py): атомарный `create`, чтение действующей сессии через `get` и идемпотентный `revoke`. В хранилище ключом служит хеш токена. Прикладной код передаёт в методы `Sessions` сам токен.
