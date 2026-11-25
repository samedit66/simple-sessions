# Your first session

[Quickstart](../README.md) · [Русский](TUTORIAL%20RU.md)

## How does the second request know who you are?

Open your orders page in a new browser and the shop asks you to sign in. Checking who you are is **authentication**. Checking whether order 123 belongs to you is **authorization**. The first check gives the shop a user ID. The orders route still needs the second check before it returns an address or a receipt.

A password is one way to establish identity. A passkey lets the application verify a signed challenge. With OpenID Connect, a provider such as your company’s identity service handles sign-in. All three can end the same way in your application: you have a verified user ID and want the browser to remember the login.

HTTP Basic sends credentials with each authenticated request. The quickstart uses it just once, at login. Another approach is a signed token such as a JWT. A service with the verification key can check its contents locally. If the service has to reject one of those tokens before it expires, it needs some additional state or coordination.

## Try it without a web server

With a server-side session, the client gets a random string called the **session ID**. Here the API calls it a token. The account ID and expiry stay on the server. Run this with Python 3.12+ after installing `simple-sessions`.

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

Here 42 identifies the account. Two logins by that account get different tokens. Only the SHA-256 digest of each token goes to the store. The original token is a credential: someone holding a copy can use it until it expires or you revoke it. Keep it out of logs and URLs.

Try removing the call to `revoke`. The last assertion now fails because the session is still active. That extra lookup on each request is both the cost and the benefit of this design. The server needs a working store, and deleting one record signs that device out on its next request. A request already in progress can finish.

## Put the session in a browser

Run the three files in the [quickstart](../README.md). Follow the cookie through the four curl commands. At login the application checks the password, then calls `auth.login(request, response, user_id)`. Look for `Set-Cookie` in the response. The browser saves the session ID and sends it back with its request to `/me`. At logout the server revokes it. The final curl command deliberately sends the old cookie again. You should get 401.

The default cookie is HttpOnly and Secure, and the session lasts one hour. Reading it leaves the original deadline in place. Cookie login and logout require POST with an allowed Origin. The default is the request’s own scheme, host and port. Behind a proxy, validate Host and make sure the application sees the public scheme through trusted proxy configuration.

If the frontend lives at another origin, pass `cookie=CookieSettings(allowed_origins=["https://app.example.com"])` to the manager. This list replaces the default. Configure credentialed CORS separately if the browser needs cross-origin requests.

## Let guests read a page

The orders page needs `require_user`: without a valid session, it returns 401. A feed may have a public version too. Add this route to the quickstart’s `users.py`.

```python
@router.get("/feed")
async def feed(request: Request, auth: Auth):
    user_id = await auth.resolve_user(request)
    return {"feed": "public" if user_id is None else "personal"}
```

Use `require_session` or `resolve_session` when you also need `created_at` or `expires_at`. The returned session already has `user_id`, so one read is enough. Optional routes still return 403 for a denied cookie origin and 503 for an unavailable store.

## Give a CLI a token

A CLI can send `Authorization: Bearer <token>` on each request. Set `app.state.auth = BearerAuth[int]()` and use `Depends(bearer_user(get_auth))` on protected routes. After verifying the caller, issue the token from the same manager that serves the routes:

```python
issued = await auth.sessions.issue(user_id, ttl=900)
response.headers["Cache-Control"] = "no-store"
return {"access_token": issued.token, "token_type": "bearer"}
```

To replace one device’s token, pass `replace_token=old_token` to `issue`. The store saves the new session and revokes the old one atomically. Cookie login already does this for the previous cookie. Other devices keep their sessions. Use `is_valid(token)` for a yes/no check, or `get_session(token)` when you also need the user ID.

## Clean up the memory store

When someone closes their browser, their session may never be read again. It still expires on time. To reclaim the memory occupied by these records, run periodic cleanup during the application's lifespan. This example sweeps the store every five minutes and waits for the task to stop on shutdown.

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

## Start a second worker

The default store lives in one process. Restart it and everyone using it signs out. Start two workers with separate memory stores and a user can log in on one worker and receive a 401 from the other. For shared sessions install `simple-sessions[redis]` and point the workers at the same Redis 7+ primary. Here the accounts use UUIDs:

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

Workers in one environment share a namespace. Give tests and production separate namespaces. The application opens and closes the Redis client. If a write times out, Redis may already have applied it, so the example disables automatic retries. Revoking the same token twice is safe.

For a different backend, implement the three methods on [SessionStore](../src/simple_sessions/store.py): atomic `create`, active-only `get`, and idempotent `revoke`. Their keys are token digests. The application-facing `Sessions` methods take the original tokens.
