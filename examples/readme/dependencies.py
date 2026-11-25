from typing import Annotated

from fastapi import Depends, Request

from simple_sessions import CookieAuth, cookie_user


async def get_auth(request: Request) -> CookieAuth[int]:
    return request.app.state.auth


type Auth = Annotated[CookieAuth[int], Depends(get_auth)]


type CurrentUserId = Annotated[int, Depends(cookie_user(get_auth))]
