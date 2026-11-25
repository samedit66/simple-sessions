import json
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis import exceptions
from redis.asyncio import Redis

from simple_sessions.models import Session, UserId, utc_now
from simple_sessions.store import SessionStoreError, SessionStoreUnavailable

# Check the deadline on the server. Redis scripts do not roll back a failed DEL.
_CREATE = """
local now = redis.call('TIME')
local now_us = tonumber(now[1]) * 1000000 + tonumber(now[2])
if tonumber(ARGV[3]) <= now_us then
    return -1
end
if KEYS[2] and not redis.acl_check_cmd('DEL', KEYS[2]) then
    return redis.error_reply('Session replacement requires DEL permission')
end
if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PXAT', ARGV[2]) then
    return 0
end
if KEYS[2] then
    redis.call('DEL', KEYS[2])
end
return 1
"""
_MICROSECONDS_PER_MILLISECOND = 1000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class RedisSessionStore[UserIdT: UserId]:
    """Share sessions across workers using a Redis 7+ primary.

    Args:
        client: Redis client whose lifetime is managed by the application.
        namespace: Key prefix shared by workers of one app and environment.
        encode_user_id: Convert an ID to its persistent string representation.
        decode_user_id: Restore the original ID type from that representation.
    """

    def __init__(
        self,
        client: Redis,
        *,
        namespace: str,
        encode_user_id: Callable[[UserIdT], str],
        decode_user_id: Callable[[str], UserIdT],
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.:-]+", namespace) is None:
            raise ValueError("Invalid Redis session namespace")
        self._client = client
        self._prefix = f"{namespace}:session:"
        self._encode_user_id = encode_user_id
        self._decode_user_id = decode_user_id

    async def create(
        self,
        digest: str,
        session: Session[UserIdT],
        *,
        replace_digest: str | None = None,
    ) -> bool:
        if digest == replace_digest:
            raise ValueError("New and predecessor digests must differ")
        keys = [self._prefix + digest]
        if replace_digest is not None:
            keys.append(self._prefix + replace_digest)
        try:
            user_id = self._encode_user_id(session.user_id)
            if not isinstance(user_id, str):
                raise TypeError("A user ID codec must return a string")
            deadline_us = (session.expires_at - _EPOCH) // timedelta(microseconds=1)
            deadline_ms = -(-deadline_us // _MICROSECONDS_PER_MILLISECOND)
            payload = json.dumps(
                [
                    user_id,
                    session.created_at.isoformat(),
                    session.expires_at.isoformat(),
                ]
            )
        except (TypeError, ValueError, OverflowError):
            raise SessionStoreError("Could not encode the session") from None
        with _redis_errors():
            result = await self._client.eval(
                _CREATE, len(keys), *keys, payload, deadline_ms, deadline_us
            )
        if result == 1:
            return True
        if result == 0:
            return False
        if result == -1:
            raise SessionStoreError("Session expired before it could be saved")
        raise SessionStoreError("Unexpected Redis session write result")

    async def get(self, digest: str) -> Session[UserIdT] | None:
        with _redis_errors():
            payload = await self._client.get(self._prefix + digest)
        if payload is None:
            return None

        try:
            fields = json.loads(payload)
            if not isinstance(fields, list) or len(fields) != 3:
                raise ValueError("Invalid session record")
            encoded_id, created, expires = fields
            if not all(isinstance(value, str) for value in fields):
                raise ValueError("Invalid session fields")
            created_at = datetime.fromisoformat(created)
            expires_at = datetime.fromisoformat(expires)
            if (
                created_at.utcoffset() != timedelta(0)
                or expires_at.utcoffset() != timedelta(0)
                or expires_at <= created_at
            ):
                raise ValueError("Invalid session timestamps")
            if expires_at <= utc_now():
                return None
            user_id = self._decode_user_id(encoded_id)
            if isinstance(user_id, bool) or not isinstance(user_id, (int, str, UUID)):
                raise TypeError("Invalid decoded user ID")
        except (TypeError, ValueError, OverflowError):
            raise SessionStoreError("Could not decode the session") from None
        return Session(user_id=user_id, created_at=created_at, expires_at=expires_at)

    async def revoke(self, digest: str) -> None:
        with _redis_errors():
            await self._client.delete(self._prefix + digest)


@contextmanager
def _redis_errors() -> Generator[None]:
    try:
        yield
    except (exceptions.AuthenticationError, exceptions.AuthorizationError) as exc:
        raise SessionStoreError("Redis session access denied") from exc
    except (exceptions.ConnectionError, exceptions.TimeoutError) as exc:
        raise SessionStoreUnavailable("Redis session store unavailable") from exc
    except (exceptions.RedisError, UnicodeDecodeError) as exc:
        raise SessionStoreError("Redis session operation failed") from exc
