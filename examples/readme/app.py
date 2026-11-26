import os
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel

from simple_sessions import CookieAuth


class UserLogin(BaseModel):
    login: str
    password: str


ADMIN_LOGIN = os.getenv("ADMIN_LOGIN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_ID = 1

app = FastAPI()
auth = CookieAuth[int]()


@app.post("/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(credentials: UserLogin, request: Request, response: Response):
    login_ok = secrets.compare_digest(credentials.login.encode(), ADMIN_LOGIN.encode())
    password_ok = secrets.compare_digest(
        credentials.password.encode(), ADMIN_PASSWORD.encode()
    )

    if not (login_ok and password_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    await auth.login(request, response, user_id=ADMIN_ID)


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response):
    await auth.logout(request, response)


@app.get("/protected")
async def protected(user_id: Annotated[int, Depends(auth.current_user)]):
    return {"user_id": user_id}
