import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from functools import cached_property
from math import floor
from typing import Annotated, ClassVar, Self

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import APIKeyCookie, HTTPBearer

from simple_sessions.models import (
    CookieSettings,
    Session,
    UserId,
    _is_origin,
    utc_now,
)
from simple_sessions.sessions import _TOKEN, Sessions
from simple_sessions.store import SessionStoreUnavailable

_BEARER = re.compile(r"bearer +([A-Za-z0-9_-]+)", re.IGNORECASE | re.ASCII)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "QUERY"})


class _RequestAuth[T: UserId](ABC):
    _challenge: ClassVar[dict[str, str] | None] = None

    def __init__(self, sessions: Sessions[T] | None = None) -> None:
        self.sessions: Sessions[T] = Sessions() if sessions is None else sessions

    async def _get_self(self) -> Self:
        return self

    @abstractmethod
    def _request_token(self, request: Request) -> str | None: ...

    async def resolve_session(self, request: Request) -> Session[T] | None:
        """Return the active session, or None for missing or invalid credentials.

        Raises:
            HTTPException: A cookie write has a denied origin (403), or the
                store is unavailable (503).
        """
        token = self._request_token(request)
        if token is None:
            return None
        with _http_store_errors():
            return await self.sessions.get_session(token)

    async def require_session(self, request: Request) -> Session[T]:
        """Return the active session or raise HTTP 401. Origin and store errors propagate."""
        session = await self.resolve_session(request)
        if session is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Authentication required",
                headers=self._challenge,
            )
        return session

    async def resolve_user(self, request: Request) -> T | None:
        """Return the user ID or None for a guest. Preserve 403 and 503 from resolve_session."""
        session = await self.resolve_session(request)
        return None if session is None else session.user_id

    async def require_user(self, request: Request) -> T:
        """Return the user ID or raise HTTP 401. Preserve 403 and 503 from require_session."""
        return (await self.require_session(request)).user_id


class CookieAuth[UserIdT: UserId](_RequestAuth[UserIdT]):
    """Manage browser login and resolve authenticated requests from a cookie.

    Pass a Sessions instance to configure storage and lifetime. Cookies
    default to Secure, HttpOnly and same-origin writes.
    """

    def __init__(
        self,
        sessions: Sessions[UserIdT] | None = None,
        *,
        cookie: CookieSettings | None = None,
    ) -> None:
        super().__init__(sessions)
        self._cookie = CookieSettings() if cookie is None else cookie

    @cached_property
    def current_user(self) -> Callable[..., Awaitable[UserIdT]]:
        """Provide the required user ID and document this manager's cookie in OpenAPI."""
        return self.user_dependency(self._get_self, cookie_name=self._cookie.name)

    @staticmethod
    def user_dependency[U: UserId](
        get_auth: "Callable[..., CookieAuth[U] | Awaitable[CookieAuth[U]]]",
        *,
        cookie_name: str = "__Host-session_id",
    ) -> Callable[..., Awaitable[U]]:
        """Provide the required user ID using the request's manager.

        Set cookie_name to the manager's cookie name for accurate OpenAPI documentation.
        """
        scheme = APIKeyCookie(
            name=cookie_name, scheme_name="SessionCookie", auto_error=False
        )
        return _user_dependency(get_auth, scheme)

    def _request_token(self, request: Request) -> str | None:
        values = _cookie_values(request, self._cookie.name)
        if len(values) != 1 or not _TOKEN.fullmatch(values[0]):
            return None
        if request.method not in _SAFE_METHODS:
            _check_origin(request, self._cookie)
        return values[0]

    async def login(
        self,
        request: Request,
        response: Response,
        user_id: UserIdT,
        *,
        ttl: int | None = None,
    ) -> Session[UserIdT]:
        """Set a fresh session cookie for a verified user and return the session.

        A successful login replaces the preceding cookie session atomically.

        Raises:
            HTTPException: The cookie is ambiguous (400), the origin is denied
                (403), the method is not POST (405), or the store is unavailable (503).
            ValueError: The TTL is invalid.
        """
        cookie = self._cookie
        _check_cookie_write(request, cookie)

        values = _cookie_values(request, cookie.name)
        if len(values) > 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ambiguous session cookie")
        old = values[0] if len(values) == 1 and _TOKEN.fullmatch(values[0]) else None
        with _http_store_errors():
            issued = await self.sessions.issue(user_id, ttl=ttl, replace_token=old)
        remaining = (issued.session.expires_at - utc_now()).total_seconds()
        response.set_cookie(
            cookie.name,
            issued.token,
            max_age=max(0, floor(remaining)),
            path="/",
            secure=cookie.secure,
            httponly=True,
            samesite=cookie.same_site,
        )
        response.headers["Cache-Control"] = "no-store"
        return issued.session

    async def logout(self, request: Request, response: Response) -> None:
        """Revoke the browser session and expire its cookie on an allowed POST."""
        cookie = self._cookie
        _check_cookie_write(request, cookie)
        values = _cookie_values(request, cookie.name)
        if len(values) == 1:
            with _http_store_errors():
                await self.sessions.revoke(values[0])
        response.delete_cookie(
            cookie.name,
            path="/",
            secure=cookie.secure,
            httponly=True,
            samesite=cookie.same_site,
        )
        response.headers["Cache-Control"] = "no-store"


class BearerAuth[T: UserId](_RequestAuth[T]):
    """Authenticate Authorization headers against the supplied Sessions instance."""

    _challenge: ClassVar[dict[str, str] | None] = {"WWW-Authenticate": "Bearer"}

    @cached_property
    def current_user(self) -> Callable[..., Awaitable[T]]:
        """Provide the required user ID and document bearer authentication in OpenAPI."""
        return self.user_dependency(self._get_self)

    @staticmethod
    def user_dependency[U: UserId](
        get_auth: "Callable[..., BearerAuth[U] | Awaitable[BearerAuth[U]]]",
    ) -> Callable[..., Awaitable[U]]:
        """Provide the required user ID using the request's bearer manager."""
        return _user_dependency(
            get_auth, HTTPBearer(scheme_name="SessionBearer", auto_error=False)
        )

    def _request_token(self, request: Request) -> str | None:
        headers = request.headers.getlist("authorization")
        match = _BEARER.fullmatch(headers[0]) if len(headers) == 1 else None
        return match[1] if match is not None and _TOKEN.fullmatch(match[1]) else None

    async def logout(self, request: Request) -> None:
        """Revoke the bearer token if present. Repeated calls are safe."""
        token = self._request_token(request)
        if token is not None:
            with _http_store_errors():
                await self.sessions.revoke(token)


def _user_dependency[T: UserId](
    get_auth: Callable[..., _RequestAuth[T] | Awaitable[_RequestAuth[T]]],
    scheme: APIKeyCookie | HTTPBearer,
) -> Callable[..., Awaitable[T]]:
    async def current_user(
        request: Request,
        auth: Annotated[_RequestAuth[T], Depends(get_auth)],
        _credential: Annotated[object, Depends(scheme)],
    ) -> T:
        return await auth.require_user(request)

    return current_user


def _cookie_values(request: Request, name: str) -> list[str]:
    # The framework's cookie dict discards duplicates.
    values = []
    for header in request.headers.getlist("cookie"):
        for pair in header.split(";"):
            key, _, value = pair.partition("=")
            if key.strip() == name:
                values.append(value.strip())
    return values


def _check_origin(request: Request, cookie: CookieSettings) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) == 1:
        origin = origins[0]
        if cookie.allowed_origins is not None:
            if origin in cookie.allowed_origins:
                return
        elif _same_origin(request, origin):
            return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin is not allowed")


def _same_origin(request: Request, origin: str) -> bool:
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        return False
    scheme = request.scope["scheme"]
    target = f"{scheme}://{hosts[0].lower()}"
    if not _is_origin(target) or not _is_origin(origin):
        return False
    default_port = ":443" if scheme == "https" else ":80"
    return target.removesuffix(default_port) == origin.removesuffix(default_port)


def _check_cookie_write(request: Request, cookie: CookieSettings) -> None:
    if request.method != "POST":
        raise HTTPException(
            status.HTTP_405_METHOD_NOT_ALLOWED,
            "Use POST for cookie login or logout",
            headers={"Allow": "POST"},
        )
    _check_origin(request, cookie)


@contextmanager
def _http_store_errors() -> Generator[None]:
    try:
        yield
    except SessionStoreUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Session store unavailable"
        ) from exc
