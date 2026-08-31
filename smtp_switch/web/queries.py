"""Read-side helpers shared by the HTML views and the JSON API."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from smtp_switch.db.models import DeliveryAttempt, Message, MessageStatus
from smtp_switch.db.session import session_scope
from smtp_switch.runtime import RuntimeContext
from smtp_switch.util import utcnow


async def queue_counts() -> dict[str, int]:
    async with session_scope() as db:
        rows = (
            await db.execute(select(Message.status, func.count()).group_by(Message.status))
        ).all()
    out = {s.value: 0 for s in MessageStatus}
    for status, count in rows:
        out[str(status)] = count
    return out


async def throughput(minutes: int = 60) -> dict[str, int]:
    since = utcnow() - timedelta(minutes=minutes)
    async with session_scope() as db:
        sent = (
            await db.execute(
                select(func.count()).select_from(Message).where(
                    Message.status == MessageStatus.SENT, Message.sent_at >= since
                )
            )
        ).scalar_one()
        received = (
            await db.execute(
                select(func.count()).select_from(Message).where(Message.received_at >= since)
            )
        ).scalar_one()
        deadlettered = (
            await db.execute(
                select(func.count()).select_from(Message).where(
                    Message.status == MessageStatus.DEADLETTER, Message.received_at >= since
                )
            )
        ).scalar_one()
    return {"window_minutes": minutes, "received": received, "sent": sent,
            "deadlettered": deadlettered}


async def provider_overview(ctx: RuntimeContext) -> list[dict[str, Any]]:
    breaker_snap = ctx.breaker.snapshot()
    out: list[dict[str, Any]] = []
    for cfg in ctx.registry.all():
        check = await ctx.rate_limiter.check(cfg.name)
        limits = cfg.limits
        limit_map = {
            "per_second": limits.per_second,
            "per_minute": limits.per_minute,
            "per_hour": limits.per_hour,
            "per_day": limits.per_day,
            "per_month": limits.per_month,
            "concurrent": limits.max_concurrent,
        }
        windows = []
        for window, limit in limit_map.items():
            if limit is None:
                continue
            room = (check.headroom or {}).get(window, limit)
            windows.append({
                "window": window,
                "limit": limit,
                "used": max(0, limit - room),
                "headroom": room,
            })
        bs = breaker_snap.get(cfg.name, {})
        out.append({
            "name": cfg.name,
            "priority": cfg.priority,
            "enabled": ctx.registry.is_enabled(cfg.name),
            "config_enabled": cfg.enabled,
            "breaker": bs.get("status", "closed"),
            "consecutive_failures": bs.get("consecutive_failures", 0),
            "last_failure_at": bs.get("last_failure_at"),
            "last_success_at": bs.get("last_success_at"),
            "inflight": ctx.rate_limiter.inflight(cfg.name),
            "accepting": check.allowed and ctx.breaker.is_available(cfg.name),
            "blocking_window": check.blocking_window,
            "windows": windows,
            "smtp_host": f"{cfg.smtp.host}:{cfg.smtp.port}",
        })
    return out


async def list_messages(
    *, status: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0
) -> list[Message]:
    stmt = select(Message).order_by(Message.received_at.desc()).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(Message.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Message.from_addr.like(like)
            | Message.subject.like(like)
            | Message.message_id_header.like(like)
        )
    async with session_scope() as db:
        return list((await db.execute(stmt)).scalars().all())


async def get_message(message_id: int) -> tuple[Message, list[DeliveryAttempt]] | None:
    async with session_scope() as db:
        msg = await db.get(Message, message_id)
        if msg is None:
            return None
        attempts = list(
            (
                await db.execute(
                    select(DeliveryAttempt)
                    .where(DeliveryAttempt.message_id == message_id)
                    .order_by(DeliveryAttempt.attempt_no)
                )
            ).scalars().all()
        )
        db.expunge(msg)
        for a in attempts:
            db.expunge(a)
        return msg, attempts
