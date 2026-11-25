import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from math import isfinite
from threading import Lock
from typing import Protocol

from simple_sessions.models import Session, UserId, utc_now


class SessionStoreError(RuntimeError):
    """The store could not complete an operation."""


class SessionStoreUnavailable(SessionStoreError):
    """The store is temporarily unreachable or an operation timed out."""


class SessionStore[UserIdT: UserId](Protocol):
    """Persist sessions by lowercase SHA-256 token digest.

    Raise SessionStoreUnavailable for temporary unavailability or timeouts
    and SessionStoreError for other storage failures."""

    async def create(
        self,
        digest: str,
        session: Session[UserIdT],
        *,
        replace_digest: str | None = None,
    ) -> bool:
        """Save the new session and remove its predecessor in one atomic operation.

        Args:
            replace_digest: A distinct predecessor digest. It may already be absent.

        Returns:
            True on insertion. An active digest collision returns False and
            leaves both records unchanged.

        Raises:
            ValueError: The new and predecessor digests are identical.
            SessionStoreError: The new session expired before insertion or
                the store could not complete the write.
        """
        ...

    async def get(self, digest: str) -> Session[UserIdT] | None:
        """Return an active session, or None if missing or expired."""
        ...

    async def revoke(self, digest: str) -> None:
        """Remove the record if present."""
        ...


class MemorySessionStore[UserIdT: UserId]:
    """Process-local sessions for testing and development."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session[UserIdT]] = {}
        self._lock = Lock()

    async def create(
        self,
        digest: str,
        session: Session[UserIdT],
        *,
        replace_digest: str | None = None,
    ) -> bool:
        if digest == replace_digest:
            raise ValueError("New and predecessor digests must differ")
        with self._lock:
            now = utc_now()
            if session.expires_at <= now:
                raise SessionStoreError("Session expired before it could be saved")
            existing = self._sessions.get(digest)
            if existing is not None and existing.expires_at > now:
                return False
            self._sessions[digest] = session
            if replace_digest is not None:
                self._sessions.pop(replace_digest, None)
            return True

    async def get(self, digest: str) -> Session[UserIdT] | None:
        with self._lock:
            session = self._sessions.get(digest)
            if session is not None and session.expires_at <= utc_now():
                del self._sessions[digest]
                return None
            return session

    async def revoke(self, digest: str) -> None:
        with self._lock:
            self._sessions.pop(digest, None)

    async def cleanup(self) -> int:
        """Remove expired records and return the number removed."""
        with self._lock:
            now = utc_now()
            expired = [
                digest
                for digest, session in self._sessions.items()
                if session.expires_at <= now
            ]
            for digest in expired:
                del self._sessions[digest]
            return len(expired)

    @asynccontextmanager
    async def periodic_cleanup(self, *, interval: float = 300) -> AsyncGenerator[None]:
        """Remove expired records periodically while the context is open.

        Args:
            interval: Positive, finite seconds between sweeps. The first sweep
                runs after one interval. Session expiry is checked on every read.
        """
        if isinstance(interval, bool) or not isfinite(interval) or interval <= 0:
            raise ValueError("Cleanup interval must be positive and finite")

        async def sweep() -> None:
            while True:
                await asyncio.sleep(interval)
                await self.cleanup()

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.wait({task})
            if not task.cancelled():
                task.result()
