"""Small shared helpers."""

from __future__ import annotations

import random
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-naive UTC now.

    We keep every stored timestamp naive-UTC so comparisons are consistent
    across SQLite (which does not preserve tzinfo) and in-memory values.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def backoff_delay(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter_ratio: float = 0.2,
) -> float:
    """Exponential backoff with full-jitter, clamped to ``max_seconds``.

    ``attempt`` is 1-based (first retry -> attempt=1).
    """
    raw = base_seconds * (2 ** max(0, attempt - 1))
    capped = min(raw, max_seconds)
    if jitter_ratio <= 0:
        return capped
    spread = capped * jitter_ratio
    return max(0.0, capped - spread + random.random() * 2 * spread)


def month_period_key(now: datetime, reset_day: int) -> str:
    """Return ``YYYY-MM`` for the monthly quota window containing ``now``.

    ``reset_day`` shifts the window boundary: if today is before ``reset_day``
    the active window is still labelled by the previous month.
    """
    if now.day >= reset_day:
        return f"{now.year:04d}-{now.month:02d}"
    year = now.year if now.month > 1 else now.year - 1
    month = now.month - 1 if now.month > 1 else 12
    return f"{year:04d}-{month:02d}"


def day_period_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")
