import os

from fastapi import FastAPI
from users import router

from simple_sessions import CookieAuth


def create_app() -> FastAPI:
    app = FastAPI()
    app.state.auth = CookieAuth[int]()
    app.state.username = os.environ["DEMO_USERNAME"]
    app.state.password = os.environ["DEMO_PASSWORD"]
    app.include_router(router)
    return app
