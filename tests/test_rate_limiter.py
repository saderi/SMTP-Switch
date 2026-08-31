import asyncio

import pytest

from smtp_switch.dispatch.rate_limiter import RateLimiter
from tests.conftest import make_settings, provider


@pytest.mark.asyncio
async def test_per_minute_window_blocks_then_check_reports_zero(tmp_path_dir):
    settings = make_settings(tmp_path_dir, [provider("p", 25, per_minute=3)])
    from smtp_switch.db import session as db_session

    db_session.init_engine(settings)
    await db_session.create_all()
    try:
        rl = RateLimiter(settings)
        reservations = []
        for _ in range(3):
            r = await rl.try_reserve("p")
            assert r is not None
            reservations.append(r)
            await rl.commit(r)

        assert await rl.try_reserve("p") is None
        check = await rl.check("p")
        assert check.allowed is False
        assert check.blocking_window == "per_minute"
        assert check.headroom["per_minute"] == 0
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_max_concurrent_enforced_and_released(tmp_path_dir):
    settings = make_settings(tmp_path_dir, [provider("p", 25, max_concurrent=2)])
    from smtp_switch.db import session as db_session

    db_session.init_engine(settings)
    await db_session.create_all()
    try:
        rl = RateLimiter(settings)
        r1 = await rl.try_reserve("p")
        r2 = await rl.try_reserve("p")
        assert r1 and r2
        assert await rl.try_reserve("p") is None  # at concurrency cap
        assert rl.inflight("p") == 2

        await rl.commit(r1)
        assert rl.inflight("p") == 1
        r3 = await rl.try_reserve("p")
        assert r3 is not None
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_release_rolls_back_quota(tmp_path_dir):
    settings = make_settings(tmp_path_dir, [provider("p", 25, per_day=2)])
    from smtp_switch.db import session as db_session

    db_session.init_engine(settings)
    await db_session.create_all()
    try:
        rl = RateLimiter(settings)
        r1 = await rl.try_reserve("p")
        assert r1 is not None
        await rl.release(r1)  # connection never reached provider

        # Quota should be back to full: two more reservations must succeed.
        assert await rl.try_reserve("p") is not None
        assert await rl.try_reserve("p") is not None
        assert await rl.try_reserve("p") is None
    finally:
        await db_session.dispose_engine()


@pytest.mark.asyncio
async def test_concurrent_reserves_do_not_oversubscribe(tmp_path_dir):
    settings = make_settings(tmp_path_dir, [provider("p", 25, per_minute=5)])
    from smtp_switch.db import session as db_session

    db_session.init_engine(settings)
    await db_session.create_all()
    try:
        rl = RateLimiter(settings)
        results = await asyncio.gather(*[rl.try_reserve("p") for _ in range(20)])
        granted = [r for r in results if r is not None]
        assert len(granted) == 5
    finally:
        await db_session.dispose_engine()
