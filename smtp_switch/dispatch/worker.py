"""The dispatcher: claim queued messages and drive them to a terminal state.

One *producer* task claims batches of due messages (flipping them to
``sending``) and feeds their ids to an ``asyncio.Queue``; ``workers`` *consumer*
tasks relay them. A single producer keeps the SQLite claim race-free without
needing ``SELECT ... FOR UPDATE``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update

from smtp_switch.config import Settings
from smtp_switch.db.models import (
    DeliveryAttempt,
    Message,
    MessageStatus,
)
from smtp_switch.db.session import session_scope
from smtp_switch.dispatch import sender
from smtp_switch.dispatch.health import CircuitBreaker
from smtp_switch.dispatch.rate_limiter import RateLimiter
from smtp_switch.dispatch.router import Router
from smtp_switch.ingress.server import SpoolWriter
from smtp_switch.logging_setup import get_logger
from smtp_switch.metrics import (
    DISPATCH_LATENCY,
    MESSAGES_DEADLETTERED,
    MESSAGES_FAILED,
    MESSAGES_SENT,
    PROVIDER_HEADROOM,
    PROVIDER_HEALTHY,
    PROVIDER_INFLIGHT,
    QUEUE_DEPTH,
)
from smtp_switch.util import backoff_delay, utcnow

log = get_logger("dispatch.worker")

_TERMINAL = {MessageStatus.SENT, MessageStatus.DEADLETTER}


class Dispatcher:
    def __init__(self, settings: Settings, registry, breaker: CircuitBreaker) -> None:
        self.settings = settings
        self.registry = registry
        self.breaker = breaker
        self.rate_limiter = RateLimiter(settings)
        self.router = Router(registry, self.rate_limiter, breaker)
        self.spool = SpoolWriter(settings.dispatch.spool_dir)
        self._queue: asyncio.Queue[int] = asyncio.Queue(
            maxsize=settings.dispatch.claim_batch_size * 2
        )
        self._wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    def notify_new_message(self) -> None:
        """Called by the ingress handler so the producer polls immediately."""
        self._wakeup.set()

    async def start(self) -> None:
        await self._recover_orphans()
        n = self.settings.dispatch.workers
        self._tasks = [
            asyncio.create_task(self._producer(), name="dispatch-producer"),
            *[
                asyncio.create_task(self._consumer(i), name=f"dispatch-worker-{i}")
                for i in range(n)
            ],
            asyncio.create_task(self._housekeeping(), name="dispatch-housekeeping"),
        ]
        log.info("dispatcher_started", workers=n)

    async def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        log.info("dispatcher_stopped")

    async def _recover_orphans(self) -> None:
        async with session_scope() as db:
            result = await db.execute(
                update(Message)
                .where(Message.status == MessageStatus.SENDING)
                .values(status=MessageStatus.QUEUED, claimed_at=None)
            )
        affected = result.rowcount  # type: ignore[attr-defined]  # CursorResult
        if affected:
            log.warning("recovered_orphaned_messages", count=affected)

    # ------------------------------------------------------------------ producer
    async def _producer(self) -> None:
        poll = self.settings.dispatch.poll_interval_seconds
        batch = self.settings.dispatch.claim_batch_size
        while not self._stopping.is_set():
            try:
                claimed = await self._claim_batch(batch)
                for mid in claimed:
                    await self._queue.put(mid)
                if not claimed:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wakeup.wait(), timeout=poll)
                    self._wakeup.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                log.error("producer_error", error=str(exc))
                await asyncio.sleep(poll)

    async def _claim_batch(self, limit: int) -> list[int]:
        now = utcnow()
        async with session_scope() as db:
            ids = (
                await db.execute(
                    select(Message.id)
                    .where(
                        Message.status == MessageStatus.QUEUED,
                        Message.next_attempt_at <= now,
                    )
                    .order_by(Message.next_attempt_at, Message.id)
                    .limit(limit)
                )
            ).scalars().all()
            if not ids:
                return []
            await db.execute(
                update(Message)
                .where(Message.id.in_(ids))
                .values(status=MessageStatus.SENDING, claimed_at=now)
            )
        return list(ids)

    # ------------------------------------------------------------------ consumer
    async def _consumer(self, index: int) -> None:
        while not self._stopping.is_set():
            try:
                mid = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._process(mid)
            except asyncio.CancelledError:
                # Put it back so a restart re-picks it promptly.
                await self._requeue_now(mid)
                raise
            except Exception as exc:  # pragma: no cover
                log.error("process_error", message_id=mid, error=str(exc))
                await self._requeue_now(mid)
            finally:
                self._queue.task_done()

    async def _requeue_now(self, message_id: int) -> None:
        with contextlib.suppress(Exception):
            async with session_scope() as db:
                await db.execute(
                    update(Message)
                    .where(Message.id == message_id, Message.status == MessageStatus.SENDING)
                    .values(status=MessageStatus.QUEUED)
                )

    # ------------------------------------------------------------------ core
    async def _process(self, message_id: int) -> None:
        async with session_scope() as db:
            msg = await db.get(Message, message_id)
            if msg is None or msg.status in _TERMINAL:
                return
            snapshot = _MsgSnapshot.from_row(msg)

        retry = self.settings.dispatch.retry
        age = utcnow() - snapshot.received_at
        expired = age > timedelta(hours=retry.max_age_hours)

        try:
            raw = self.spool.read(snapshot.spool_path)
        except FileNotFoundError:
            await self._deadletter(message_id, "spool_missing", None)
            return

        if expired:
            await self._deadletter(message_id, "expired", "message exceeded max age")
            return

        started = time.monotonic()
        tried: set[str] = set()
        made_attempt = False
        saw_transient = saw_connect = saw_permanent = False
        last_error: str | None = None
        last_code: int | None = None

        for _ in range(self.settings.dispatch.failover_per_tick):
            decision = await self.router.select(exclude=frozenset(tried))
            if decision is None:
                break
            provider = decision.provider
            tried.add(provider)
            made_attempt = True
            attempt_no = snapshot.attempts + len(tried)

            attempt_started = utcnow()
            result = await sender.relay(
                self.registry.get(provider),
                mail_from=snapshot.from_addr,
                rcpt_tos=snapshot.rcpt_to,
                raw_message=raw,
            )
            await self._record_attempt(
                message_id, attempt_no, provider, attempt_started, result
            )
            last_error = result.detail
            last_code = result.smtp_code

            if result.ok:
                await self.rate_limiter.commit(decision.reservation)
                await self.breaker.record_success(provider)
                await self._mark_sent(message_id, provider, snapshot.claimed_at)
                MESSAGES_SENT.labels(provider=provider).inc()
                DISPATCH_LATENCY.observe(time.monotonic() - started)
                log.info("message_sent", message_id=message_id, provider=provider,
                         attempt=attempt_no)
                return

            MESSAGES_FAILED.labels(provider=provider, result=result.classification).inc()

            if result.classification == sender.CONNECT_ERROR:
                saw_connect = True
                await self.rate_limiter.release(decision.reservation)
                await self.breaker.record_failure(provider, error=result.detail)
            elif result.classification == sender.TRANSIENT:
                saw_transient = True
                await self.rate_limiter.commit(decision.reservation)
                await self.breaker.record_failure(provider, error=result.detail)
            else:  # PERMANENT
                saw_permanent = True
                await self.rate_limiter.commit(decision.reservation)
                # The provider is healthy; the message was rejected.
                await self.breaker.record_success(provider)

        await self._handle_no_success(
            message_id,
            snapshot,
            made_attempt=made_attempt,
            tried=tried,
            saw_transient=saw_transient,
            saw_connect=saw_connect,
            saw_permanent=saw_permanent,
            last_error=last_error,
            last_code=last_code,
        )

    async def _handle_no_success(
        self,
        message_id: int,
        snapshot: _MsgSnapshot,
        *,
        made_attempt: bool,
        tried: set[str],
        saw_transient: bool,
        saw_connect: bool,
        saw_permanent: bool,
        last_error: str | None,
        last_code: int | None,
    ) -> None:
        retry = self.settings.dispatch.retry

        if not made_attempt:
            # Every provider was down or capped — hold without burning an attempt.
            hold = float(self.settings.dispatch.no_capacity_backoff_seconds)
            await self._reschedule(message_id, hold, last_error="no provider capacity",
                                   bump_attempts=False)
            log.info("message_held_no_capacity", message_id=message_id, retry_in_s=hold)
            return

        all_candidates = set(self.router.candidate_names())
        exhausted = all_candidates.issubset(tried)
        only_permanent = saw_permanent and not saw_transient and not saw_connect

        if only_permanent and exhausted:
            await self._deadletter(message_id, "rejected", last_error, code=last_code)
            return

        next_attempts = snapshot.attempts + 1
        if next_attempts >= retry.max_attempts:
            await self._deadletter(message_id, "max_attempts", last_error, code=last_code)
            return

        delay = backoff_delay(
            next_attempts,
            base_seconds=retry.base_delay_seconds,
            max_seconds=retry.max_delay_seconds,
            jitter_ratio=retry.jitter_ratio,
        )
        # Don't schedule past the message's hard deadline.
        deadline = snapshot.received_at + timedelta(hours=retry.max_age_hours)
        if utcnow() + timedelta(seconds=delay) >= deadline:
            await self._deadletter(message_id, "expired", last_error, code=last_code)
            return

        await self._reschedule(message_id, delay, last_error=last_error, bump_attempts=True)
        log.info("message_requeued", message_id=message_id, attempt=next_attempts,
                 retry_in_s=round(delay, 1), tried=sorted(tried))

    # ------------------------------------------------------------------ db writes
    async def _record_attempt(
        self, message_id: int, attempt_no: int, provider: str,
        started_at, result: sender.RelayResult,
    ) -> None:
        async with session_scope() as db:
            db.add(
                DeliveryAttempt(
                    message_id=message_id,
                    attempt_no=attempt_no,
                    provider=provider,
                    started_at=started_at,
                    finished_at=utcnow(),
                    result=result.classification,
                    smtp_code=result.smtp_code,
                    error=result.detail,
                )
            )

    async def _mark_sent(self, message_id: int, provider: str, claimed_at) -> None:
        async with session_scope() as db:
            await db.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    status=MessageStatus.SENT,
                    provider_used=provider,
                    sent_at=utcnow(),
                    last_error=None,
                )
            )

    async def _reschedule(
        self, message_id: int, delay_seconds: float, *, last_error: str | None,
        bump_attempts: bool,
    ) -> None:
        async with session_scope() as db:
            values = {
                "status": MessageStatus.QUEUED,
                "next_attempt_at": utcnow() + timedelta(seconds=delay_seconds),
                "last_error": (last_error or "")[:2000] or None,
                "claimed_at": None,
            }
            if bump_attempts:
                values["attempts"] = Message.attempts + 1
            await db.execute(update(Message).where(Message.id == message_id).values(**values))

    async def _deadletter(
        self, message_id: int, reason: str, detail: str | None, *, code: int | None = None,
    ) -> None:
        async with session_scope() as db:
            await db.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    status=MessageStatus.DEADLETTER,
                    last_error=f"[{reason}] {detail or ''}".strip()[:2000],
                    claimed_at=None,
                )
            )
        MESSAGES_DEADLETTERED.labels(reason=reason).inc()
        log.warning("message_deadlettered", message_id=message_id, reason=reason,
                    code=code, detail=(detail or "")[:200])

    # ------------------------------------------------------------------ housekeeping
    async def _housekeeping(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(60)
                pruned = await self.rate_limiter.prune()
                await self._refresh_gauges()
                deleted = await self._retention_sweep()
                if pruned or deleted:
                    log.debug("housekeeping", send_log_pruned=pruned, messages_deleted=deleted)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                log.warning("housekeeping_error", error=str(exc))

    async def _refresh_gauges(self) -> None:
        async with session_scope() as db:
            rows = (
                await db.execute(
                    select(Message.status, func.count()).group_by(Message.status)
                )
            ).all()
        by_status = dict.fromkeys(("queued", "sending", "sent", "deadletter"), 0)
        for status, count in rows:
            by_status[str(status)] = count
        for status, count in by_status.items():
            QUEUE_DEPTH.labels(status=status).set(count)

        for cfg in self.registry.all():
            PROVIDER_HEALTHY.labels(provider=cfg.name).set(
                1 if self.breaker.is_available(cfg.name) else 0
            )
            PROVIDER_INFLIGHT.labels(provider=cfg.name).set(
                self.rate_limiter.inflight(cfg.name)
            )
            check = await self.rate_limiter.check(cfg.name)
            for window, room in (check.headroom or {}).items():
                PROVIDER_HEADROOM.labels(provider=cfg.name, window=window).set(room)

    async def _retention_sweep(self) -> int:
        d = self.settings.dispatch
        now = utcnow()
        deleted = 0
        plans = []
        if d.sent_retention_hours:
            plans.append((MessageStatus.SENT, now - timedelta(hours=d.sent_retention_hours)))
        if d.deadletter_retention_hours:
            plans.append(
                (MessageStatus.DEADLETTER, now - timedelta(hours=d.deadletter_retention_hours))
            )
        for status, cutoff in plans:
            async with session_scope() as db:
                rows = (
                    await db.execute(
                        select(Message.id, Message.spool_path)
                        .where(Message.status == status, Message.received_at < cutoff)
                        .limit(500)
                    )
                ).all()
                if not rows:
                    continue
                ids = [r[0] for r in rows]
                await db.execute(
                    delete(DeliveryAttempt).where(DeliveryAttempt.message_id.in_(ids))
                )
                await db.execute(delete(Message).where(Message.id.in_(ids)))
            for _mid, spath in rows:
                self.spool.delete(spath)
            deleted += len(ids)
        return deleted


@dataclass(slots=True)
class _MsgSnapshot:
    """Plain-data copy of a Message, safe to use after its session closes."""

    id: int
    received_at: datetime
    from_addr: str
    rcpt_to: list[str]
    spool_path: str
    attempts: int
    claimed_at: datetime | None

    @classmethod
    def from_row(cls, msg: Message) -> _MsgSnapshot:
        return cls(
            id=msg.id,
            received_at=msg.received_at,
            from_addr=msg.from_addr,
            rcpt_to=list(msg.rcpt_to or []),
            spool_path=msg.spool_path,
            attempts=msg.attempts,
            claimed_at=msg.claimed_at,
        )
