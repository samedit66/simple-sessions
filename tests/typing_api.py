from typing import assert_type
from uuid import UUID

from fastapi import Request

from simple_sessions import (
    BearerAuth,
    CookieAuth,
    CookieSettings,
    IssuedSession,
    Session,
    Sessions,
    bearer_user,
    cookie_user,
)


async def check_types(
    auth: CookieAuth[UUID] | BearerAuth[UUID], request: Request, user_id: UUID
) -> None:
    assert_type(auth.sessions, Sessions[UUID])
    assert_type(await auth.sessions.issue(user_id), IssuedSession[UUID])
    assert_type(await auth.require_user(request), UUID)
    assert_type(await auth.resolve_user(request), UUID | None)
    assert_type(await auth.require_session(request), Session[UUID])
    assert_type(await auth.resolve_session(request), Session[UUID] | None)


def check_origin_inputs(origin: str) -> None:
    settings = CookieSettings(allowed_origins=[origin])
    assert_type(settings.allowed_origins, frozenset[str] | None)
    CookieSettings(allowed_origins=(origin,))
    CookieSettings(allowed_origins={origin})
    CookieSettings(allowed_origins=frozenset({origin}))


async def check_dependencies(request: Request) -> None:
    async def get_cookie() -> CookieAuth[UUID]:
        return CookieAuth[UUID]()

    def get_bearer() -> BearerAuth[UUID]:
        return BearerAuth[UUID]()

    assert_type(await cookie_user(get_cookie)(request), UUID)
    assert_type(await bearer_user(get_bearer)(request), UUID)
