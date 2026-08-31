from datetime import datetime

from smtp_switch.util import backoff_delay, day_period_key, month_period_key


def test_backoff_is_monotonic_and_capped():
    delays = [
        backoff_delay(n, base_seconds=10, max_seconds=300, jitter_ratio=0)
        for n in range(1, 8)
    ]
    assert delays[0] == 10
    assert delays[1] == 20
    assert delays[2] == 40
    assert all(d <= 300 for d in delays)
    assert delays[-1] == 300  # capped


def test_backoff_jitter_stays_in_band():
    for _ in range(200):
        d = backoff_delay(3, base_seconds=10, max_seconds=300, jitter_ratio=0.2)
        # attempt 3 -> raw 40, +/-20%
        assert 32 <= d <= 48


def test_day_period_key():
    assert day_period_key(datetime(2026, 8, 31, 5, 0)) == "2026-08-31"


def test_month_period_key_default_reset():
    assert month_period_key(datetime(2026, 8, 31), reset_day=1) == "2026-08"
    assert month_period_key(datetime(2026, 8, 1), reset_day=1) == "2026-08"


def test_month_period_key_custom_reset_day():
    # Billing month starts on the 15th.
    assert month_period_key(datetime(2026, 8, 20), reset_day=15) == "2026-08"
    assert month_period_key(datetime(2026, 8, 10), reset_day=15) == "2026-07"
    assert month_period_key(datetime(2026, 1, 5), reset_day=15) == "2025-12"
