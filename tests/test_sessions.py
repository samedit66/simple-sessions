import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from simple_sessions import MemorySessionStore, Sessions, SessionStoreError
from simple_sessions import sessions as sessions_module
from simple_sessions import store as store_module

pytestmark = pytest.mark.asyncio


async def test_issue_returns_a_secret_but_stores_only_its_digest():
    store = MemorySessionStore[int]()
    sessions = Sessions(store)
    issued = await sessions.issue(19, ttl=30)

    assert issued.session.user_id == 19
    assert issued.session.created_at.tzinfo == UTC
    assert issued.session.expires_at - issued.session.created_at == timedelta(
        seconds=30
    )
    assert issued.token not in repr(issued)
    assert "token" not in issued.model_dump()
    assert issued.token not in issued.model_dump_json()
    assert len(issued.token) == 43
    assert await store.get(issued.token) is None
    assert await store.get(sha256(issued.token.encode()).hexdigest()) == issued.session
    assert await sessions.get_session(issued.token) == issued.session


async def test_replacement_retries_collision_without_revoking_the_predecessor(
    monkeypatch,
):
    old, other, new = "A" * 43, "B" * 42 + "A", "C" * 42 + "A"
    tokens = iter([old, other, other, old, new])
    monkeypatch.setattr(sessions_module, "token_urlsafe", lambda _: next(tokens))
    sessions = Sessions[int]()
    await sessions.issue(1)
    await sessions.issue(2)

    assert (await sessions.issue(1, replace_token=old)).token == new
    assert not await sessions.is_valid(old)
    assert await sessions.is_valid(other)
    assert await sessions.is_valid(new)

    await sessions.revoke(new)
    await sessions.revoke(new)
    assert not await sessions.is_valid(new)


async def test_exhausted_collisions_leave_the_previous_session_active(monkeypatch):
    old, collision = "A" * 43, "B" * 42 + "A"
    sessions = Sessions[int]()
    tokens = iter([old, collision])
    monkeypatch.setattr(sessions_module, "token_urlsafe", lambda _: next(tokens))
    await sessions.issue(1)
    await sessions.issue(2)

    draws = []

    def repeat_token(size):
        draws.append(size)
        return collision

    monkeypatch.setattr(sessions_module, "token_urlsafe", repeat_token)
    with pytest.raises(SessionStoreError, match="unique"):
        await sessions.issue(1, replace_token=old)
    assert len(draws) == 3
    assert await sessions.is_valid(old)
    assert await sessions.is_valid(collision)


async def test_exact_expiry_and_cleanup(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(sessions_module, "utc_now", lambda: now)
    monkeypatch.setattr(store_module, "utc_now", lambda: now)
    store = MemorySessionStore[int]()
    sessions = Sessions(store, ttl=10)
    read_then_expire = await sessions.issue(1)
    unvisited = await sessions.issue(2)
    long_lived = await sessions.issue(3, ttl=20)

    now += timedelta(seconds=9)
    assert await sessions.is_valid(read_then_expire.token)
    now += timedelta(seconds=1)
    assert not await sessions.is_valid(read_then_expire.token)
    assert await store.cleanup() == 1
    assert await store.cleanup() == 0
    assert not await sessions.is_valid(unvisited.token)
    assert await sessions.is_valid(long_lived.token)


@pytest.mark.parametrize("bad_ttl", [0, -1, True, 1.5, 10**30])
async def test_invalid_ttl_does_not_revoke(bad_ttl):
    sessions = Sessions[int]()
    old = await sessions.issue(1)
    with pytest.raises(ValueError, match="TTL"):
        await sessions.issue(1, ttl=bad_ttl, replace_token=old.token)
    assert await sessions.is_valid(old.token)


@pytest.mark.parametrize("token", ["", "☀", "a" * 44, "A" * 42 + "B", "A" * 43 + "="])
async def test_malformed_token_is_not_a_session(token):
    sessions = Sessions[int]()
    assert await sessions.get_session(token) is None
    await sessions.revoke(token)
    with pytest.raises(ValueError, match="replacement"):
        await sessions.issue(1, replace_token=token)


async def test_uuid_is_preserved_and_stale_predecessor_is_allowed():
    sessions = Sessions()
    user_id = uuid4()
    issued = await sessions.issue(user_id, replace_token="A" * 43)
    assert (await sessions.get_session(issued.token)).user_id == user_id


async def test_periodic_cleanup_repeats_and_joins_worker(monkeypatch):
    store = MemorySessionStore[int]()
    sessions = Sessions(store)
    await sessions.issue(1, ttl=10)
    now = store_module.utc_now() + timedelta(seconds=10)
    monkeypatch.setattr(store_module, "utc_now", lambda: now)
    cleanup = store.cleanup
    worker = None
    sweeps = []
    repeated = asyncio.Event()

    async def observe_cleanup():
        nonlocal worker
        worker = asyncio.current_task()
        sweeps.append(await cleanup())
        if len(sweeps) == 2:
            repeated.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(store, "cleanup", observe_cleanup)
    with pytest.raises(RuntimeError, match="shutdown"):
        async with store.periodic_cleanup(interval=0.001):
            await asyncio.wait_for(repeated.wait(), timeout=2)
            raise RuntimeError("shutdown")

    assert worker.cancelled()
    assert sweeps == [1, 0]


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True])
async def test_periodic_cleanup_rejects_invalid_interval(interval):
    with pytest.raises(ValueError, match="interval"):
        async with MemorySessionStore[int]().periodic_cleanup(interval=interval):
            pytest.fail("Invalid interval accepted")
