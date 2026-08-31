"""Per-provider, multi-window rate limiter.

A *reservation* is taken **before** the outbound SMTP conversation starts, so
two concurrent workers can never both squeeze past the same limit. The single
process holds one :class:`asyncio.Lock` per provider, which is sufficient for the
single-instance deployment this project targets.

After the relay:

* :meth:`RateLimiter.commit` — the provider saw the message (any SMTP reply,
  2xx/4xx/5xx). The ``send_log`` row and quota increments stay; only the
  in-memory concurrency slot is freed.
* :meth:`RateLimiter.release` — the connection never reached the provider
  (DNS/connect/TLS failure). The reservation is rolled back entirely so it does
  not consume the provider's quota.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select

from smtp_switch.config import ProviderConfig, Settings
from smtp_switch.db.models import ProviderQuota, SendLogEntry
from smtp_switch.db.session import session_scope
from smtp_switch.logging_setup import get_logger
from smtp_switch.util import day_period_key, month_period_key, utcnow

log = get_logger("dispatch.rate_limiter")


@dataclass(slots=True)
class Reservation:
    provider: str
    send_log_id: int
    day_key: str | None
    month_key: str | None
    reset_day: int


@dataclass(slots=True)
class LimitCheck:
    allowed: bool
    blocking_window: str | None = None
    headroom: dict[str, int] | None = None


class RateLimiter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._inflight: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------ helpers
    def inflight(self, provider: str) -> int:
        return self._inflight[provider]

    def _provider(self, name: str) -> ProviderConfig:
        cfg = self._settings.provider(name)
        if cfg is None:
            raise KeyError(f"unknown provider: {name}")
        return cfg

    async def _sliding_counts(self, session, provider: str, limits) -> dict[str, int]:
        now = utcnow()
        counts: dict[str, int] = {}
        for window, (span, _limit) in limits.sliding_windows.items():
            since = now - timedelta(seconds=span)
            counts[window] = (
                await session.execute(
                    select(func.count())
                    .select_from(SendLogEntry)
                    .where(SendLogEntry.provider == provider, SendLogEntry.sent_at >= since)
                )
            ).scalar_one()
        return counts

    async def _quota_count(self, session, provider: str, scope: str, key: str) -> int:
        row = await session.get(ProviderQuota, (provider, key, scope))
        return row.count if row else 0

    # ------------------------------------------------------------------- public
    async def check(self, provider: str) -> LimitCheck:
        """Non-mutating: would a send be allowed right now, and how much headroom?"""
        cfg = self._provider(provider)
        limits = cfg.limits
        headroom: dict[str, int] = {}
        blocking: str | None = None

        if limits.max_concurrent is not None:
            room = limits.max_concurrent - self._inflight[provider]
            headroom["concurrent"] = max(0, room)
            if room <= 0:
                blocking = blocking or "concurrent"

        async with session_scope() as session:
            counts = await self._sliding_counts(session, provider, limits)
            for window, (_span, limit) in limits.sliding_windows.items():
                room = limit - counts.get(window, 0)
                headroom[window] = max(0, room)
                if room <= 0:
                    blocking = blocking or window

            now = utcnow()
            if limits.per_day is not None:
                used = await self._quota_count(session, provider, "day", day_period_key(now))
                room = limits.per_day - used
                headroom["per_day"] = max(0, room)
                if room <= 0:
                    blocking = blocking or "per_day"
            if limits.per_month is not None:
                key = month_period_key(now, limits.month_reset_day)
                used = await self._quota_count(session, provider, "month", key)
                room = limits.per_month - used
                headroom["per_month"] = max(0, room)
                if room <= 0:
                    blocking = blocking or "per_month"

        return LimitCheck(allowed=blocking is None, blocking_window=blocking, headroom=headroom)

    async def try_reserve(self, provider: str) -> Reservation | None:
        cfg = self._provider(provider)
        limits = cfg.limits
        async with self._locks[provider]:
            if (
                limits.max_concurrent is not None
                and self._inflight[provider] >= limits.max_concurrent
            ):
                return None

            now = utcnow()
            async with session_scope() as session:
                counts = await self._sliding_counts(session, provider, limits)
                for window, (_span, limit) in limits.sliding_windows.items():
                    if counts.get(window, 0) >= limit:
                        return None

                day_key = month_key = None
                if limits.per_day is not None:
                    day_key = day_period_key(now)
                    if await self._quota_count(session, provider, "day", day_key) >= limits.per_day:
                        return None
                if limits.per_month is not None:
                    month_key = month_period_key(now, limits.month_reset_day)
                    used = await self._quota_count(session, provider, "month", month_key)
                    if used >= limits.per_month:
                        return None

                entry = SendLogEntry(provider=provider, sent_at=now)
                session.add(entry)
                await session.flush()
                if day_key is not None:
                    await self._bump_quota(session, provider, "day", day_key)
                if month_key is not None:
                    await self._bump_quota(session, provider, "month", month_key)
                send_log_id = entry.id

            self._inflight[provider] += 1
            return Reservation(
                provider=provider,
                send_log_id=send_log_id,
                day_key=day_key,
                month_key=month_key,
                reset_day=limits.month_reset_day,
            )

    async def commit(self, reservation: Reservation) -> None:
        async with self._locks[reservation.provider]:
            self._inflight[reservation.provider] = max(
                0, self._inflight[reservation.provider] - 1
            )

    async def release(self, reservation: Reservation) -> None:
        """Roll a reservation back — the provider never received the message."""
        async with self._locks[reservation.provider]:
            self._inflight[reservation.provider] = max(
                0, self._inflight[reservation.provider] - 1
            )
            async with session_scope() as session:
                await session.execute(
                    delete(SendLogEntry).where(SendLogEntry.id == reservation.send_log_id)
                )
                if reservation.day_key is not None:
                    await self._bump_quota(
                        session, reservation.provider, "day", reservation.day_key, delta=-1
                    )
                if reservation.month_key is not None:
                    await self._bump_quota(
                        session, reservation.provider, "month", reservation.month_key, delta=-1
                    )

    async def _bump_quota(
        self, session, provider: str, scope: str, key: str, delta: int = 1
    ) -> None:
        row = await session.get(ProviderQuota, (provider, key, scope))
        if row is None:
            row = ProviderQuota(provider=provider, period_key=key, scope=scope, count=0)
            session.add(row)
        row.count = max(0, row.count + delta)
        row.updated_at = utcnow()

    async def prune(self) -> int:
        """Delete ``send_log`` rows older than the widest sliding window in use."""
        widest = max(
            (p.limits.max_window_span for p in self._settings.providers),
            default=0,
        )
        if widest <= 0:
            return 0
        cutoff = utcnow() - timedelta(seconds=widest + 60)
        async with session_scope() as session:
            result = await session.execute(
                delete(SendLogEntry).where(SendLogEntry.sent_at < cutoff)
            )
        return result.rowcount or 0  # type: ignore[attr-defined]  # CursorResult
