import pytest

from smtp_switch.config import CircuitBreakerConfig
from smtp_switch.dispatch.health import CircuitBreaker
from smtp_switch.dispatch.rate_limiter import RateLimiter
from smtp_switch.dispatch.router import Router
from smtp_switch.providers.registry import ProviderRegistry
from tests.conftest import make_settings, provider


async def _build(tmp_path_dir, providers):
    settings = make_settings(tmp_path_dir, providers)
    from smtp_switch.db import session as db_session

    db_session.init_engine(settings)
    await db_session.create_all()
    registry = ProviderRegistry(settings)
    await registry.refresh_overrides()
    rl = RateLimiter(settings)
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=60))
    return settings, db_session, registry, rl, cb, Router(registry, rl, cb)


@pytest.mark.asyncio
async def test_picks_lowest_priority_number_first(tmp_path_dir):
    _, db_session, *_rest, router = await _build(
        tmp_path_dir,
        [provider("a", 1, priority=20), provider("b", 2, priority=10)],
    )
    try:
        decision = await router.select()
        assert decision is not None
        assert decision.provider == "b"
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_fails_over_when_primary_capped(tmp_path_dir):
    _, db_session, registry, rl, cb, router = await _build(
        tmp_path_dir,
        [provider("a", 1, priority=10, per_minute=1), provider("b", 2, priority=20)],
    )
    try:
        first = await router.select()
        assert first.provider == "a"
        await rl.commit(first.reservation)
        # 'a' now has zero headroom for the minute -> next pick is 'b'
        second = await router.select()
        assert second.provider == "b"
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_skips_open_breaker(tmp_path_dir):
    _, db_session, registry, rl, cb, router = await _build(
        tmp_path_dir,
        [provider("a", 1, priority=10), provider("b", 2, priority=20)],
    )
    try:
        await cb.record_failure("a")
        await cb.record_failure("a")  # threshold 2 -> open
        decision = await router.select()
        assert decision.provider == "b"
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_exclude_set_is_honoured(tmp_path_dir):
    _, db_session, *_rest, router = await _build(
        tmp_path_dir,
        [provider("a", 1, priority=10), provider("b", 2, priority=20)],
    )
    try:
        decision = await router.select(exclude=frozenset({"a"}))
        assert decision.provider == "b"
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_returns_none_when_all_capped(tmp_path_dir):
    _, db_session, registry, rl, cb, router = await _build(
        tmp_path_dir,
        [provider("a", 1, per_minute=1), provider("b", 2, per_minute=1)],
    )
    try:
        d1 = await router.select()
        await rl.commit(d1.reservation)
        d2 = await router.select()
        await rl.commit(d2.reservation)
        assert await router.select() is None
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_disabled_override_removes_provider(tmp_path_dir):
    _, db_session, registry, rl, cb, router = await _build(
        tmp_path_dir,
        [provider("a", 1, priority=10), provider("b", 2, priority=20)],
    )
    try:
        await registry.set_enabled("a", False)
        decision = await router.select()
        assert decision.provider == "b"
    finally:
        await db_session.dispose_engine()
