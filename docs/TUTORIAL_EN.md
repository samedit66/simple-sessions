# Server-side sessions with FastAPI

[Quickstart](../README.md) · [Русский](TUTORIAL_RU.md)

Your app verifies a password, passkey or identity-provider response and gets a user ID. How does the next request remember that login? The client keeps a random token. The server keeps the user ID and expiry, and can revoke that login independently of the user’s other devices.

We’ll build from the store up. You can run the storage and token examples without a web server. For the HTTP examples, put the snippets in one Python 3.12+ project.

```sh
uv add simple-sessions
```

With pip, use `python -m pip install simple-sessions`.

## SessionStore — where a login lives

A store receives a lowercase SHA-256 token digest and a `Session` containing `user_id`, `created_at` and `expires_at`. Implement these three async methods to plug in another database. The protocol uses structural typing, so a class with matching methods already qualifies.

| Method | Guarantee |
| --- | --- |
| `create(digest, session, *, replace_digest=None)` | Insert and optionally remove the predecessor atomically. A missing predecessor is fine. Return `True` on success. An active collision returns `False` and preserves both records. An expired row can be reused. |
| `get(digest)` | Return an active session, or `None` for an unknown or expired digest. Expiry applies even before cleanup runs. |
| `revoke(digest)` | Remove the row if present. Repeating the call is safe. |

Identical new and predecessor digests raise `ValueError`. An incoming session that already expired raises `SessionStoreError`. Use its subclass `SessionStoreUnavailable` for timeouts and temporary outages. A timed-out write may already have committed. [Store contract](../src/simple_sessions/store.py)

### MemorySessionStore for a single process

`Sessions()` creates its own memory store. Create one explicitly to sweep expired records. `cleanup()` runs once and returns the number removed. The context manager waits one interval before its first sweep and stops the task on exit.

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

### Keep sessions in a SQLite file

Save this adapter as `sqlite_store.py`. It uses Python’s standard library and supports integer user IDs. Each operation opens its own connection in a worker thread. A writer obtains the database lock **before** checking the clock or touching either row. The connection context commits the whole rotation or rolls it back.

The database stores UTC timestamps in a fixed-width format so text comparisons preserve chronological order. Use a file on a local persistent volume and give every worker the same path.

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

Try reopening the same file with a fresh manager. Save this next to the adapter and run it with `uv run python demo.py`. To reclaim expired rows, a maintenance job can delete rows with `expires_at <= ?` using the same UTC timestamp format.

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

### RedisSessionStore for multiple hosts

For Redis 7+, install the optional extra with `uv add 'simple-sessions[redis]'` (pip: `python -m pip install 'simple-sessions[redis]'`). Set `REDIS_URL` to your primary. Workers use the same namespace within an environment. Redis expires the keys. The codecs preserve the application’s user ID type, here a UUID. The app owns the client and closes it on shutdown.

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

## Sessions — issue, look up, rotate and revoke

Pass any compatible store to `Sessions(store, ttl=...)`. Each login gets its own token. The default lifetime is one hour. Set a positive whole number of seconds on the manager or override it on one issuance. Reading a session leaves its deadline in place.

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

`Session` and `IssuedSession` are frozen models. An issued session exposes the plaintext credential through `.token` for delivery to the client. Its repr and model dump omit that field. Keep it out of URLs and logs. IDs can be `int`, `str`, or `UUID`. A malformed, unknown or expired token resolves to `None`. Passing a malformed predecessor to `issue` raises `ValueError`; a well-formed predecessor may already be gone.

## CookieAuth — sign a browser in and out

Save the following as `app.py`. This small demo reads one account from the environment. In your app, replace the credential check with your own account lookup. Once you have a verified ID, `login` issues a session, rotates the previous cookie and writes the new cookie to the response. `logout` revokes the session and expires the cookie. Both responses set `Cache-Control: no-store`.

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

Start the demo and try the four curl commands in the [README](../README.md#cookie-based-authentication). With pip, install `uvicorn` and run the same command without `uv run --with uvicorn`.

```sh
ADMIN_LOGIN=alice ADMIN_PASSWORD=local-demo uv run --with uvicorn uvicorn app:app
```

### CookieSettings and Origin

By default the cookie is named `__Host-session_id` and has `Secure`, `HttpOnly`, `SameSite=lax` and `Path=/`. Login and logout require POST and an allowed Origin. Other cookie-authenticated writes also check Origin. The default matches the request’s scheme, host and port. Behind a proxy, configure trusted forwarded scheme information and validate Host.

To allow a separate frontend, replace the manager declaration in the example. An explicit allowlist replaces the default, so include every origin that needs write access. Configure credentialed CORS separately for cross-origin browser calls.

```python
from simple_sessions import CookieSettings

settings = CookieSettings(
    name="__Host-shop_session",
    same_site="lax",
    allowed_origins=["https://shop.example", "https://account.example"],
)
auth = CookieAuth[int](cookie=settings)
```

`same_site` accepts `"lax"`, `"strict"` or `"none"`. The last requires Secure. For local plain HTTP use `CookieSettings(name="dev_session", secure=False)`. Cookie login with duplicate session cookies returns 400, a denied Origin returns 403, and calling cookie login or logout with the wrong method returns 405.

### Required users, guests and session details

Both transports expose the following request methods. Pick the smallest result your handler needs. Required methods raise HTTP 401 for missing or invalid credentials. Optional methods return `None` for a guest. Both propagate HTTP 503 for temporary store failures and the cookie Origin check can still raise 403.

| Result | Required | Optional |
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

### Declare the manager on a shared router

`Depends(auth.current_user)` binds one instance and describes its cookie in OpenAPI. If the same router serves several apps, use `user_dependency(get_auth)` to select the manager per request. Add this to the cookie example. With a custom cookie, also pass its name as `cookie_name` to the factory so the schema matches.

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

## BearerAuth — give an API client a token

Add these routes to the same `app.py`. They reuse the credential check and give API clients their own session manager. To use SQLite or Redis, pass your initialized `Sessions(store)` to either transport. Managers backed by the same store and namespace can look up the same tokens.

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

Restart the demo after adding the routes. Ask for a token, read your account, then revoke it. The final request returns 401 with `WWW-Authenticate: Bearer`. A shared router can declare `Depends(BearerAuth.user_dependency(get_bearer))`, where `get_bearer` returns that app’s bearer manager.

```sh
url=http://localhost:8000
token=$(curl -fsS -H 'Content-Type: application/json' \
  -d '{"login":"alice","password":"local-demo"}' "$url/token" | \
  python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
curl -i -H "Authorization: Bearer $token" "$url/api/me"
curl -i -X POST -H "Authorization: Bearer $token" "$url/api/logout"
curl -i -H "Authorization: Bearer $token" "$url/api/me"
```
