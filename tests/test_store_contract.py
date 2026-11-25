import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import AuthenticationError, ConnectionError

from simple_sessions import (
    MemorySessionStore,
    Session,
    Sessions,
    SessionStoreError,
    SessionStoreUnavailable,
)
from simple_sessions.redis import RedisSessionStore

pytestmark = pytest.mark.asyncio


def redis_store(client, namespace):
    return RedisSessionStore(
        client, namespace=namespace, encode_user_id=str, decode_user_id=UUID
    )


@asynccontextmanager
async def redis_connections():
    url = os.environ.get("REDIS_TEST_URL")
    if not url:
        pytest.skip("REDIS_TEST_URL is required for Redis integration checks")
    options = {"socket_timeout": 3, "retry": Retry(NoBackoff(), 0)}
    async with (
        Redis.from_url(url, **options) as writer,
        Redis.from_url(url, **options) as reader,
    ):
        yield writer, reader, f"simple-sessions-test:{uuid4()}"


@pytest_asyncio.fixture
async def redis_clients():
    async with redis_connections() as clients:
        yield clients


@pytest_asyncio.fixture(params=["memory", "redis"])
async def stores(request):
    if request.param == "memory":
        store = MemorySessionStore[UUID]()
        yield store, store
    else:
        async with redis_connections() as (writer, reader, namespace):
            yield redis_store(writer, namespace), redis_store(reader, namespace)


def session(*, ttl=60):
    now = datetime.now(UTC)
    return Session(
        user_id=uuid4(), created_at=now, expires_at=now + timedelta(seconds=ttl)
    )


async def test_replacement_and_collision_contract(stores):
    writer, reader = stores
    old, occupied, fresh = "a" * 64, "b" * 64, "c" * 64
    first, other, replacement = session(), session(), session()

    assert await writer.create(old, first)
    assert await writer.create(occupied, other)
    assert not await writer.create(occupied, replacement, replace_digest=old)
    assert await reader.get(old) == first
    assert await reader.get(occupied) == other

    assert await writer.create(fresh, replacement, replace_digest=old)
    assert await reader.get(old) is None
    assert await reader.get(fresh) == replacement
    await writer.revoke(fresh)
    await writer.revoke(fresh)
    assert await reader.get(fresh) is None
    assert await reader.get(occupied) == other
    await writer.revoke(occupied)


async def test_concurrent_insert_has_one_winner(stores):
    first, second = stores
    digest = "a" * 64
    one, two = session(), session()
    results = await asyncio.gather(
        first.create(digest, one), second.create(digest, two)
    )
    assert sorted(results) == [False, True]
    assert await second.get(digest) == (one if results[0] else two)
    await second.revoke(digest)
    assert await first.get(digest) is None


async def test_expired_replacement_preserves_previous_session(stores):
    writer, reader = stores
    old, new = "a" * 64, "b" * 64
    assert await writer.create(old, session())
    yesterday = datetime.now(UTC) - timedelta(days=1)
    expired = Session(
        user_id=uuid4(),
        created_at=yesterday,
        expires_at=yesterday + timedelta(seconds=60),
    )

    with pytest.raises(SessionStoreError, match="expired"):
        await writer.create(new, expired, replace_digest=old)
    assert await reader.get(old) is not None
    assert await reader.get(new) is None
    await writer.revoke(old)


async def test_redis_issues_native_ttl_and_another_client_can_revoke(redis_clients):
    writer, reader, namespace = redis_clients
    first = Sessions(redis_store(writer, namespace))
    second = Sessions(redis_store(reader, namespace))
    token = await first.issue(uuid4(), ttl=2)
    digest = sha256(token.token.encode()).hexdigest()
    key = f"{namespace}:session:{digest}"
    key_deadline_ms = await reader.pexpiretime(key)
    key_deadline = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        milliseconds=key_deadline_ms
    )
    assert (
        timedelta(0)
        <= key_deadline - token.session.expires_at
        < timedelta(milliseconds=1)
    )
    assert await reader.exists(f"{namespace}:session:{token.token}") == 0
    assert await second.get_session(token.token) == token.session

    async def wait_for_expiry():
        while await reader.exists(key):
            await asyncio.sleep(0.025)

    await asyncio.wait_for(wait_for_expiry(), timeout=5)
    assert not await first.is_valid(token.token)

    active = await first.issue(uuid4())
    await second.revoke(active.token)
    assert not await first.is_valid(active.token)


async def test_redis_corrupt_record_is_a_store_error(redis_clients):
    writer, _, namespace = redis_clients
    digest = "a" * 64
    key = f"{namespace}:session:{digest}"
    await writer.set(key, b"invalid payload", ex=30)
    with pytest.raises(SessionStoreError, match="decode") as error:
        await redis_store(writer, namespace).get(digest)
    assert not isinstance(error.value, SessionStoreUnavailable)
    await writer.delete(key)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (ConnectionError, SessionStoreUnavailable),
        (AuthenticationError, SessionStoreError),
    ],
)
async def test_redis_classifies_connection_and_authentication_errors(
    monkeypatch, cause, expected
):
    async with Redis() as client:

        async def fail(*args, **kwargs):
            raise cause("test")

        monkeypatch.setattr(client, "execute_command", fail)
        with pytest.raises(expected) as error:
            await redis_store(client, "test").get("a" * 64)
        assert type(error.value) is expected
