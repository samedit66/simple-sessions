from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from simple_sessions import (
    BearerAuth,
    CookieAuth,
    CookieSettings,
    MemorySessionStore,
    Sessions,
    SessionStoreError,
    SessionStoreUnavailable,
    bearer_user,
    cookie_user,
)

ORIGIN = "https://app.example"
COOKIE = "__Host-session_id"


def make_app(auth: CookieAuth[int] | BearerAuth[int], user_id: int = 7) -> FastAPI:
    app = FastAPI()

    app.state.auth = auth

    async def get_auth(request: Request):
        return request.app.state.auth

    current_user = (
        cookie_user(get_auth) if isinstance(auth, CookieAuth) else bearer_user(get_auth)
    )

    @app.post("/issue")
    async def issue():
        return {"token": (await auth.sessions.issue(user_id)).token}

    if isinstance(auth, CookieAuth):

        @app.post("/login", status_code=status.HTTP_204_NO_CONTENT)
        @app.get("/login", include_in_schema=False)
        async def login(request: Request, response: Response):
            await auth.login(request, response, user_id)

    @app.api_route("/me", methods=["QUERY"], include_in_schema=False)
    @app.get("/me")
    async def me(user: Annotated[int, Depends(current_user)]):
        return {"user": user}

    @app.get("/hello")
    @app.post("/hello")
    async def hello(user: Annotated[int | None, Depends(auth.resolve_user)]):
        return {"user": user}

    @app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    @app.get("/logout", include_in_schema=False)
    async def logout(request: Request, response: Response):
        if isinstance(auth, CookieAuth):
            await auth.logout(request, response)
        else:
            await auth.logout(request)

    return app


def client_for(app, *, raise_app_exceptions=True):
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url=ORIGIN,
    )


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_bearer_sign_in_and_out_with_two_isolated_apps():
    async with (
        client_for(make_app(BearerAuth[int](), 7)) as first,
        client_for(make_app(BearerAuth[int](), 9)) as second,
    ):
        missing = await first.get("/me")
        assert missing.status_code == status.HTTP_401_UNAUTHORIZED
        assert missing.headers["WWW-Authenticate"] == "Bearer"
        assert (await first.get("/hello")).json() == {
            "user": None,
        }

        token = (await first.post("/issue")).json()["token"]
        assert (await first.get("/me", headers=bearer(token))).json() == {
            "user": 7,
        }
        assert (
            await second.get("/me", headers=bearer(token))
        ).status_code == status.HTTP_401_UNAUTHORIZED
        assert (
            await second.get(
                "/me", headers=bearer((await second.post("/issue")).json()["token"])
            )
        ).json() == {
            "user": 9,
        }
        assert (
            await first.post("/logout", headers=bearer(token))
        ).status_code == status.HTTP_204_NO_CONTENT
        assert (
            await first.post("/logout", headers=bearer(token))
        ).status_code == status.HTTP_204_NO_CONTENT
        assert (
            await first.get("/me", headers=bearer(token))
        ).status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_cookie_login_rotation_and_logout():
    auth = CookieAuth[int]()
    async with client_for(make_app(auth)) as client:
        issued = await auth.sessions.issue(7)
        assert (
            await client.get("/me", headers=bearer(issued.token))
        ).status_code == 401
        assert (await client.post("/login")).status_code == status.HTTP_403_FORBIDDEN
        assert (await client.get("/login")).headers["allow"] == "POST"

        first = await client.post("/login", headers={"origin": ORIGIN})
        assert first.status_code == status.HTTP_204_NO_CONTENT
        assert first.headers["cache-control"] == "no-store"
        assert {"secure", "httponly", "samesite=lax", "path=/"} <= {
            part.strip().lower() for part in first.headers["set-cookie"].split(";")[1:]
        }
        old_token = client.cookies[COOKIE]
        second = await client.post("/login", headers={"origin": ORIGIN})
        assert second.status_code == status.HTTP_204_NO_CONTENT
        assert client.cookies[COOKIE] != old_token
        assert (
            await client.get("/me", headers={"cookie": f"{COOKIE}={old_token}"})
        ).status_code == status.HTTP_401_UNAUTHORIZED

        assert (await client.get("/me", headers=bearer("unused"))).json()["user"] == 7
        assert (await client.post("/hello", headers={"origin": ORIGIN})).json()[
            "user"
        ] == 7
        assert (await client.post("/hello")).status_code == status.HTTP_403_FORBIDDEN
        assert (
            await client.post("/hello", headers={"origin": "null"})
        ).status_code == status.HTTP_403_FORBIDDEN
        assert (
            await client.post(
                "/hello", headers=[("origin", ORIGIN), ("origin", ORIGIN)]
            )
        ).status_code == status.HTTP_403_FORBIDDEN
        assert (
            await client.post("/logout", headers={"origin": "https://wrong.example"})
        ).status_code == status.HTTP_403_FORBIDDEN
        revoked = client.cookies[COOKIE]
        logout = await client.post("/logout", headers={"origin": ORIGIN})
        assert logout.status_code == status.HTTP_204_NO_CONTENT
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert COOKIE not in client.cookies
        response = await client.get("/me", headers={"cookie": f"{COOKIE}={revoked}"})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "header",
    [
        [("authorization", "")],
        [("authorization", "Basic ignored")],
        [("authorization", "Bearer malformed")],
        [("authorization", "Bearer ignored"), ("authorization", "Bearer ignored")],
        [("authorization", "Bearer ignored, Bearer ignored")],
    ],
)
@pytest.mark.asyncio
async def test_bearer_rejects_invalid_headers_even_with_a_valid_cookie(header):
    auth = BearerAuth[int]()
    token = (await auth.sessions.issue(7)).token
    async with client_for(make_app(auth)) as client:
        client.cookies.set(COOKIE, token)
        assert (await client.get("/me", headers=header)).status_code == 401
        assert (await client.get("/hello", headers=header)).json()["user"] is None


@pytest.mark.asyncio
async def test_duplicate_cookies_are_ambiguous_for_lookup_and_login():
    auth = CookieAuth[int](cookie=CookieSettings(allowed_origins=frozenset({ORIGIN})))
    async with client_for(make_app(auth)) as client:
        assert (
            await client.post("/login", headers={"origin": ORIGIN})
        ).status_code == status.HTTP_204_NO_CONTENT
        token = client.cookies[COOKIE]
        pair = f"{COOKIE}={token}"
        assert (
            await client.get("/me", headers={"cookie": f"{pair}; {pair}"})
        ).status_code == status.HTTP_401_UNAUTHORIZED
        headers = [("cookie", pair), ("cookie", pair), ("origin", ORIGIN)]
        assert (
            await client.post("/login", headers=headers)
        ).status_code == status.HTTP_400_BAD_REQUEST
        assert (await client.get("/me")).status_code == status.HTTP_200_OK


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (SessionStoreUnavailable, status.HTTP_503_SERVICE_UNAVAILABLE),
        (SessionStoreError, status.HTTP_500_INTERNAL_SERVER_ERROR),
    ],
)
@pytest.mark.asyncio
async def test_store_failure_is_not_anonymous_or_success(error, expected_status):
    class FailedStore(MemorySessionStore[int]):
        async def create(self, *args, **kwargs):
            raise error()

        async def get(self, digest):
            raise error()

        async def revoke(self, digest):
            raise error()

    auth = CookieAuth(
        Sessions(FailedStore()),
        cookie=CookieSettings(allowed_origins=frozenset({ORIGIN})),
    )
    async with client_for(make_app(auth), raise_app_exceptions=False) as client:
        header = {"cookie": f"{COOKIE}={'A' * 43}"}
        for endpoint in ["/hello", "/me"]:
            assert (
                await client.get(endpoint, headers=header)
            ).status_code == expected_status

        login = await client.post("/login", headers={"origin": ORIGIN})
        assert login.status_code == expected_status
        assert "set-cookie" not in login.headers

        logout = await client.post(
            "/logout", headers={"origin": ORIGIN, "cookie": f"{COOKIE}={'A' * 43}"}
        )
        assert logout.status_code == expected_status
        assert "set-cookie" not in logout.headers


@pytest.mark.parametrize(
    ("auth", "scheme", "model"),
    [
        (
            CookieAuth[int](),
            "SessionCookie",
            {"in": "cookie", "name": COOKIE, "type": "apiKey"},
        ),
        (BearerAuth[int](), "SessionBearer", {"type": "http", "scheme": "bearer"}),
    ],
)
def test_openapi_documents_only_required_routes(auth, scheme, model):
    schema = make_app(auth).openapi()
    assert schema["components"]["securitySchemes"] == {scheme: model}
    assert schema["paths"]["/me"]["get"]["security"] == [{scheme: []}]
    assert "security" not in schema["paths"]["/hello"]["get"]


@pytest.mark.asyncio
async def test_custom_cookie_name_for_static_router():
    name = "__Host-shop_session"
    auth = CookieAuth[int](cookie=CookieSettings(name=name))
    router = APIRouter()

    async def get_auth(request: Request) -> CookieAuth[int]:
        return request.app.state.auth

    @router.get("/me")
    async def me(
        user: Annotated[int, Depends(cookie_user(get_auth, cookie_name=name))],
    ):
        return user

    app = FastAPI()
    app.state.auth = auth
    app.include_router(router)
    assert (
        app.openapi()["components"]["securitySchemes"]["SessionCookie"]["name"] == name
    )
    token = (await auth.sessions.issue(7)).token
    async with client_for(app) as client:
        assert (
            await client.get("/me", headers={"cookie": f"{COOKIE}={token}"})
        ).status_code == 401
        assert (
            await client.get("/me", headers={"cookie": f"{name}={token}"})
        ).json() == 7


@pytest.mark.asyncio
async def test_shared_router_uses_manager_from_each_app():
    router = APIRouter()

    async def get_auth(request: Request) -> BearerAuth[int]:
        return request.app.state.auth

    current_user = bearer_user(get_auth)

    @router.get("/me")
    async def me(user_id: Annotated[int, Depends(current_user)]):
        return user_id

    def create_app():
        app = FastAPI()
        app.state.auth = BearerAuth[int]()
        app.include_router(router)
        return app

    first, second = create_app(), create_app()
    token = (await first.state.auth.sessions.issue(7)).token
    async with client_for(first) as a, client_for(second) as b:
        assert (await a.get("/me", headers=bearer(token))).json() == 7
        assert (await b.get("/me", headers=bearer(token))).status_code == 401
        second_token = (await second.state.auth.sessions.issue(9)).token
        assert (await b.get("/me", headers=bearer(second_token))).json() == 9


@pytest.mark.parametrize(
    "origin", ["*", "null", "https://x.test/path", "https://x.test:65536"]
)
def test_invalid_origin_configuration(origin):
    with pytest.raises(ValueError, match="origins"):
        CookieSettings(allowed_origins=frozenset({origin}))


def test_cookie_configuration_requires_secure_for_host_prefix_and_none():
    with pytest.raises(ValueError, match="Secure"):
        CookieSettings(allowed_origins=frozenset({ORIGIN}), secure=False)
    with pytest.raises(ValueError, match="Secure"):
        CookieSettings(
            allowed_origins=frozenset({ORIGIN}),
            name="sid",
            secure=False,
            same_site="none",
        )


@pytest.mark.asyncio
async def test_same_origin_and_explicit_override():
    async with client_for(make_app(CookieAuth[int]())) as client:
        for origin in ("http://app.example", ORIGIN + ":8443", "https://other.example"):
            assert (
                await client.post("/login", headers={"origin": origin})
            ).status_code == 403
        assert (
            await client.post(
                "/login", headers={"origin": ORIGIN, "host": "APP.EXAMPLE:443"}
            )
        ).status_code == 204

    frontend = "https://frontend.example"
    auth = CookieAuth[int](cookie=CookieSettings(allowed_origins=[frontend]))
    async with client_for(make_app(auth)) as client:
        assert (
            await client.post("/login", headers={"origin": ORIGIN})
        ).status_code == 403
        assert (
            await client.post("/login", headers={"origin": frontend})
        ).status_code == 204


def test_cookie_settings_parse_origin_lists_and_freeze_the_result():
    settings = CookieSettings.model_validate({"allowed_origins": [ORIGIN, ORIGIN]})

    assert settings.allowed_origins == frozenset({ORIGIN})
    assert settings.secure is True
    with pytest.raises(ValidationError, match="frozen"):
        settings.secure = False


@pytest.mark.asyncio
async def test_cookie_query_requires_session_but_not_origin():
    auth = CookieAuth[int](cookie=CookieSettings(allowed_origins=frozenset({ORIGIN})))
    async with client_for(make_app(auth)) as client:
        anonymous = await client.request("QUERY", "/me")
        assert anonymous.status_code == status.HTTP_401_UNAUTHORIZED

        login = await client.post("/login", headers={"origin": ORIGIN})
        assert login.status_code == status.HTTP_204_NO_CONTENT
        response = await client.request("QUERY", "/me", json={"filter": "active"})
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"user": 7}
