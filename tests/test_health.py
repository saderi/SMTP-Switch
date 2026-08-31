from datetime import timedelta

import pytest

from smtp_switch.config import CircuitBreakerConfig
from smtp_switch.dispatch.health import CircuitBreaker
from smtp_switch.util import utcnow


def _force_cooldown_elapsed(cb: CircuitBreaker, provider: str) -> None:
    """Backdate ``opened_at`` so the breaker is past its cooldown → half-open."""
    st = cb._st(provider)
    if st.opened_at is not None:
        st.opened_at = utcnow() - timedelta(seconds=cb._cfg.cooldown_seconds + 1)


@pytest.mark.asyncio
async def test_opens_after_threshold_and_blocks(db):
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=60))
    for _ in range(2):
        await cb.record_failure("p")
    assert cb.is_available("p") is True
    await cb.record_failure("p")  # third strike
    assert cb.status("p") == "open"
    assert cb.is_available("p") is False


@pytest.mark.asyncio
async def test_half_open_after_cooldown_then_close_on_success(db):
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60))
    await cb.record_failure("p")
    assert cb.status("p") == "open"
    _force_cooldown_elapsed(cb, "p")
    assert cb.status("p") == "half_open"
    assert await cb.begin_probe("p") is True
    assert await cb.begin_probe("p") is False  # only one probe allowed
    await cb.record_success("p")
    assert cb.status("p") == "closed"
    assert cb.is_available("p") is True


@pytest.mark.asyncio
async def test_half_open_probe_failure_reopens(db):
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60))
    await cb.record_failure("p")
    _force_cooldown_elapsed(cb, "p")
    assert await cb.begin_probe("p") is True
    await cb.record_failure("p")  # probe failed
    assert cb.status("p") == "open"
    assert cb._st("p").half_open_probes == 0


@pytest.mark.asyncio
async def test_state_persists_across_reload(db):
    cfg = CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=600)
    cb = CircuitBreaker(cfg)
    await cb.record_failure("p")
    assert cb.status("p") == "open"

    cb2 = CircuitBreaker(cfg)
    await cb2.load()
    assert cb2.status("p") == "open"
    assert cb2.is_available("p") is False


@pytest.mark.asyncio
async def test_success_resets_failure_count(db):
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=60))
    await cb.record_failure("p")
    await cb.record_failure("p")
    await cb.record_success("p")
    assert cb._st("p").consecutive_failures == 0
    await cb.record_failure("p")
    assert cb.is_available("p") is True  # only 1 strike since reset
