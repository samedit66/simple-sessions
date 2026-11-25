import re
from datetime import timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from simple_sessions.models import IssuedSession, Session, UserId, utc_now
from simple_sessions.store import MemorySessionStore, SessionStore, SessionStoreError

_TOKEN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_TOKEN_ATTEMPTS = 3
_TOKEN_BYTES = 32
_DEFAULT_TTL_SECONDS = 3600


class Sessions[UserIdT: UserId]:
    """Issue, look up and revoke server-side session tokens.

    Args:
        store: Omit to create a process-local store for this instance.
        ttl: Default lifetime in seconds for newly issued tokens.
    """

    def __init__(
        self,
        store: SessionStore[UserIdT] | None = None,
        *,
        ttl: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._store: SessionStore[UserIdT] = (
            MemorySessionStore() if store is None else store
        )
        self._ttl = _validate_ttl(ttl)

    async def issue(
        self,
        user_id: UserIdT,
        *,
        ttl: int | None = None,
        replace_token: str | None = None,
    ) -> IssuedSession[UserIdT]:
        """Issue a fresh token after the application has verified the user's identity.

        Args:
            ttl: Override the manager's lifetime in seconds for this issuance.
            replace_token: Atomically revoke this token when saving the new
                session. A well-formed token may already be absent or expired.

        Returns:
            The session and its plaintext token for delivery to the client.

        Raises:
            ValueError: The lifetime or replacement token is invalid.
            TypeError: The user ID is outside the supported types.
            SessionStoreError: Issuance failed. A timed-out write may have committed.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, (int, str, UUID)):
            raise TypeError("user_id must be an int, str or UUID")
        duration = self._ttl if ttl is None else _validate_ttl(ttl)
        predecessor = None if replace_token is None else _token_digest(replace_token)
        if replace_token is not None and predecessor is None:
            raise ValueError("Invalid replacement token")

        now = utc_now()
        try:
            session = Session(
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(seconds=duration),
            )
        except OverflowError as exc:
            raise ValueError("TTL exceeds the supported date range") from exc

        for _ in range(_TOKEN_ATTEMPTS):
            token = token_urlsafe(_TOKEN_BYTES)
            digest = sha256(token.encode("ascii")).hexdigest()
            if digest != predecessor and await self._store.create(
                digest, session, replace_digest=predecessor
            ):
                return IssuedSession(session=session, token=token)
        raise SessionStoreError("Could not allocate a unique session token")

    async def get_session(self, token: str) -> Session[UserIdT] | None:
        """Return the active session, or None for a malformed, unknown or expired token."""
        digest = _token_digest(token)
        return None if digest is None else await self._store.get(digest)

    async def is_valid(self, token: str) -> bool:
        """Check whether the token currently identifies an active session."""
        return await self.get_session(token) is not None

    async def revoke(self, token: str) -> None:
        """Invalidate the token for future requests. Repeated revocation is safe."""
        digest = _token_digest(token)
        if digest is not None:
            await self._store.revoke(digest)


def _validate_ttl(ttl: int) -> int:
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ValueError("TTL must be a positive integer in seconds")
    return ttl


def _token_digest(token: str) -> str | None:
    if _TOKEN.fullmatch(token) is None:
        return None
    return sha256(token.encode("ascii")).hexdigest()
