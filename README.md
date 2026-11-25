# Simple Sessions

[English tutorial](docs/TUTORIAL_EN.md) · [Туториал на русском](docs/TUTORIAL_RU.md)

Give your FastAPI app sessions you can revoke. Issue a token after sign-in, read the user ID in a route, and revoke the token on sign-out.

## Try cookie sessions

Save these three files in one directory. The demo signs in one user (ID 1) with credentials read at startup. Sessions use the default secure cookie and a one-hour lifetime.

**`app.py`**

```python
import os

from fastapi import FastAPI

from simple_sessions import CookieAuth
from users import router


def create_app() -> FastAPI:
    app = FastAPI()
    app.state.auth = CookieAuth[int]()
    app.state.username = os.environ["DEMO_USERNAME"]
    app.state.password = os.environ["DEMO_PASSWORD"]
    app.include_router(router)
    return app
```

**`dependencies.py`**

```python
from typing import Annotated

from fastapi import Depends, Request

from simple_sessions import CookieAuth, cookie_user


async def get_auth(request: Request) -> CookieAuth[int]:
    return request.app.state.auth


Auth = Annotated[CookieAuth[int], Depends(get_auth)]


CurrentUserId = Annotated[int, Depends(cookie_user(get_auth))]
```

**`users.py`**

```python
from secrets import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from dependencies import Auth, CurrentUserId

router = APIRouter()


@router.post("/login", status_code=204)
async def login(
    request: Request,
    response: Response,
    credentials: Annotated[HTTPBasicCredentials, Depends(HTTPBasic())],
    auth: Auth,
) -> None:
    state = request.app.state
    username_ok = compare_digest(credentials.username.encode(), state.username.encode())
    password_ok = compare_digest(credentials.password.encode(), state.password.encode())
    if not (username_ok and password_ok):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    await auth.login(request, response, user_id=1)


@router.get("/me")
async def me(user_id: CurrentUserId) -> dict[str, int]:
    return {"user_id": user_id}


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, auth: Auth) -> None:
    await auth.logout(request, response)
```

With Python 3.12+, install the dependencies and start the app:

```sh
python -m pip install simple-sessions uvicorn
export DEMO_USERNAME=alice DEMO_PASSWORD=local-demo
uvicorn app:create_app --factory
```

In another terminal, enter the configured password when curl prompts. Curl supports Secure cookies on `http://localhost`. The last request reuses the saved cookie after revocation.

```sh
url=http://localhost:8000
curl -i -c cookies.txt -u alice -H "Origin: $url" -X POST "$url/login"  # 204
curl -i -b cookies.txt "$url/me"                                       # 200
curl -i -b cookies.txt -H "Origin: $url" -X POST "$url/logout"         # 204
curl -i -b cookies.txt "$url/me"                                       # 401
```

FastAPI documents `/me` as cookie-protected through `cookie_user(get_auth)`.
For a custom name, pass the same configuration value to `CookieSettings(name=...)`
and `cookie_user(get_auth, cookie_name=...)`. For a Bearer API, store
`BearerAuth[int]()` in the app and declare protected routes with `bearer_user(get_auth)`.

For an application with an accounts database, replace the demo credential check with your password verifier and pass the verified account ID to `auth.login`. For multiple workers, configure a shared store as shown in the [tutorial](docs/TUTORIAL_EN.md).

## Run the tests

```sh
uv sync --locked --all-extras
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync mypy
```

Set `REDIS_TEST_URL` to include real Redis integration tests. The tests use isolated key namespaces. See [Releasing](RELEASING.md) for package checks and publication.
