from secrets import compare_digest
from typing import Annotated

from dependencies import Auth, CurrentUserId
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

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
