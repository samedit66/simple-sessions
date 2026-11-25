import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

type UserId = int | str | UUID

_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+")
_ORIGIN = re.compile(r"https?://(?:[a-z0-9.-]+|\[[a-f0-9:.]+\])(?::[0-9]+)?")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Session[UserIdT: UserId](BaseModel):
    """The identity and lifetime of a server-side session."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    user_id: UserIdT
    created_at: datetime
    expires_at: datetime


class IssuedSession[UserIdT: UserId](BaseModel):
    """The issued session and plaintext token. Repr and model dumps omit the token."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    session: Session[UserIdT]
    token: str = Field(repr=False, exclude=True)


class CookieSettings(BaseModel):
    """Configure the browser cookie and the origins allowed to make writes.

    Args:
        allowed_origins: Omit to require the request's own scheme, host and port.
            An explicit collection replaces this default. Include the application's
            origin in the collection if it should also be allowed.
        secure: Set to False for local HTTP with an unprefixed cookie name.
            SameSite "none" requires Secure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_origins: frozenset[str] | None = Field(default=None, min_length=1)
    name: str = "__Host-session_id"
    secure: StrictBool = True
    same_site: Literal["lax", "strict", "none"] = "lax"

    if TYPE_CHECKING:
        # Pydantic accepts these inputs and stores a frozenset after validation.
        def __init__(
            self,
            *,
            allowed_origins: list[str]
            | tuple[str, ...]
            | set[str]
            | frozenset[str]
            | None = ...,
            name: str = ...,
            secure: bool = ...,
            same_site: Literal["lax", "strict", "none"] = ...,
        ) -> None: ...

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, origins: frozenset[str] | None) -> frozenset[str] | None:
        if origins is not None and not all(_is_origin(origin) for origin in origins):
            raise ValueError("allowed_origins must contain explicit HTTP(S) origins")
        return origins

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        if not _COOKIE_NAME.fullmatch(name):
            raise ValueError("Invalid session cookie name")
        return name

    @model_validator(mode="after")
    def validate_secure_cookie(self) -> Self:
        secure_name = self.name.lower().startswith(("__host-", "__secure-", "__http-"))
        if not self.secure and (secure_name or self.same_site == "none"):
            raise ValueError("The cookie name or SameSite setting requires Secure")
        return self


def _is_origin(origin: str) -> bool:
    if not _ORIGIN.fullmatch(origin):
        return False
    try:
        return urlsplit(origin).port != 0
    except ValueError:
        return False
