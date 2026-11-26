<div align="center">

# simple-sessions

[![CI](https://github.com/samedit66/simple-sessions/actions/workflows/ci-and-publish.yml/badge.svg)](https://github.com/samedit66/simple-sessions/actions/workflows/ci-and-publish.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python)](https://www.python.org/downloads/)
[![Apache 2.0 license](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

Issue a token after sign-in, read the user ID in a route, and revoke the token on sign-out.

</div>

## What you get

Add session-based authentication to your FastAPI app with a few lines of code. Your app checks who’s signing in. Once you have their user ID, `simple-sessions` takes care of issuing, looking up, and revoking their session.

- **`CookieAuth`** sets a cookie that the browser sends with later requests.
- **`BearerAuth`** reads a token that an API client sends in the `Authorization` header.

Both keep the session on the server. The client gets a random token, and the store keeps its SHA-256 digest. Pick the type your app already uses for user IDs: `int`, `str`, or `UUID`.

## Cheat sheet

Add the package to your Python 3.12+ project:

```sh
uv add simple-sessions
```

With pip, run `python -m pip install simple-sessions` in your virtual environment.

### Cookie-based authentication

Create one manager for your app. Here our user IDs are integers:

```python
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from simple_sessions import CookieAuth

app = FastAPI()
auth = CookieAuth[int]()


class UserLogin(BaseModel):
    login: str
    password: str
```

In the login route, use your app’s credential check. Here, `verify_credentials` stands for a function that returns the verified account’s ID, or `None` if the credentials are wrong:

```python
@app.post("/login", status_code=204)
async def login(credentials: UserLogin, request: Request, response: Response):
    user_id = await verify_credentials(credentials)
    if user_id is None:
        raise HTTPException(status_code=401)
    await auth.login(request, response, user_id=user_id)
```

`login` sets a Secure, HttpOnly cookie and replaces the session from the previous cookie, if present. To protect a route, ask for `auth.current_user`. FastAPI documents the cookie in OpenAPI, and the handler gets the user ID. A missing or expired session returns HTTP 401.

```python
@app.get("/protected")
async def protected(user_id: Annotated[int, Depends(auth.current_user)]):
    return {"user_id": user_id}


@app.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    await auth.logout(request, response)
```

Want to try the whole flow? The [runnable example](examples/readme/app.py) includes a single-account credential check. From the repository root, start the example with uv:

```sh
ADMIN_LOGIN=alice ADMIN_PASSWORD=local-demo uv run uvicorn examples.readme.app:app
```

With pip, install the checkout and Uvicorn using `python -m pip install -e . uvicorn`, then run the same command without `uv run`.

In another terminal, send the same `Origin` that a browser would send on login and logout. Save the cookie from the first response and try it again after signing out:

```sh
url=http://localhost:8000
curl -i -c cookies.txt -H "Origin: $url" -H 'Content-Type: application/json' \
  -d '{"login":"alice","password":"local-demo"}' "$url/login"       # 204
curl -i -b cookies.txt "$url/protected"                            # 200
curl -i -b cookies.txt -H "Origin: $url" -X POST "$url/logout"     # 204
curl -i -b cookies.txt "$url/protected"                            # 401
```

### Using a shared router

If you keep your routes in a shared router, set `app.state.auth = auth` when you create each app. Then put the following dependency in your `dependencies.py` module. Each request uses its own app’s manager:

```python
from typing import Annotated

from fastapi import Depends, Request

from simple_sessions import CookieAuth


def get_auth(request: Request) -> CookieAuth[int]:
    return request.app.state.auth


type CurrentUser = Annotated[int, Depends(CookieAuth.user_dependency(get_auth))]
```

Import `CurrentUser` in your routes and use it as the parameter type: `async def protected(user_id: CurrentUser)`. If you use a custom cookie name, pass the same `cookie_name` to `user_dependency` so OpenAPI shows the right name.

### Bearer-based authentication

For a CLI or API client, create a `BearerAuth` manager. After the same credential check as above, issue a token and return it to the client:

```python
from simple_sessions import BearerAuth

bearer = BearerAuth[int]()


@app.post("/token")
async def token(credentials: UserLogin, response: Response):
    user_id = await verify_credentials(credentials)
    if user_id is None:
        raise HTTPException(status_code=401)
    issued = await bearer.sessions.issue(user_id)
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": issued.token, "token_type": "bearer"}


@app.get("/me")
async def me(user_id: Annotated[int, Depends(bearer.current_user)]):
    return {"user_id": user_id}
```

Send `Authorization: Bearer <access_token>` on each protected request. To sign out, send the same header to a route that calls `await bearer.logout(request)`.

## Other features

### Let guests in

A feed can welcome guests and still show signed-in users their own content. Use the cookie manager from the quickstart:

```python
@app.get("/feed")
async def feed(request: Request):
    user_id = await auth.resolve_user(request)
    if user_id is None:
        return {"feed": "public"}
    return {"feed": "personal", "user_id": user_id}
```

### Set a lifetime and check when a session expires

Sessions last one hour by default. For a 15-minute session, pass the lifetime in seconds when you issue it. In the cookie login route, after checking the credentials:

```python
await auth.login(request, response, user_id=user_id, ttl=900)
```

For the bearer token route, use `issued = await bearer.sessions.issue(user_id, ttl=900)`. To show a signed-in user how long their session lasts, read the session itself:

```python
@app.get("/session")
async def session_details(request: Request):
    session = await auth.require_session(request)
    return {"created_at": session.created_at, "expires_at": session.expires_at}
```

### Share sessions across workers

The default store lives in one process and starts empty after a restart. To share sessions across workers, point them at the same Redis 7+ primary and use the same namespace. Add Redis support to your project:

```sh
uv add 'simple-sessions[redis]'
```

With pip, run `python -m pip install 'simple-sessions[redis]'`.

Set `REDIS_URL` to your Redis connection URL. This example creates one client per worker and closes it at shutdown. The shared router above can pick up the manager through `get_auth`:

```python
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from simple_sessions import CookieAuth, Sessions
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
            client, namespace="my-app:prod", encode_user_id=str, decode_user_id=int
        )
        app.state.auth = CookieAuth(Sessions(store))
        yield


app = FastAPI(lifespan=lifespan)
```

Have your own store? Implement `create`, `get`, and `revoke` as described in [SessionStore](src/simple_sessions/store.py).

For more on cookie origins, expiry, and Redis setup, read the [English tutorial](docs/TUTORIAL_EN.md) or the [Russian tutorial](docs/TUTORIAL_RU.md).
