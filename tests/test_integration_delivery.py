"""End-to-end: real ingress SMTP server + dispatcher + fake upstream providers."""

from __future__ import annotations

import asyncio

import aiosmtplib
import pytest
from sqlalchemy import select

from smtp_switch.db import session as db_session
from smtp_switch.db.models import DeliveryAttempt, Message, MessageStatus
from smtp_switch.main import Application
from tests.conftest import make_settings, provider
from tests.fakeprovider import FakeProvider

pytestmark = pytest.mark.asyncio


async def _send(port: int, *, subject: str = "hi", to: str = "dest@example.com") -> tuple:
    msg = (
        f"From: sender@example.com\r\nTo: {to}\r\n"
        f"Subject: {subject}\r\nMessage-ID: <{subject}@example.com>\r\n\r\nbody\r\n"
    )
    return await aiosmtplib.send(
        msg.encode(), sender="sender@example.com", recipients=[to],
        hostname="127.0.0.1", port=port,
    )


async def _wait_status(message_id: int, status: str, timeout: float = 8.0) -> Message:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with db_session.session_scope() as db:
            row = await db.get(Message, message_id)
            if row is not None and row.status == status:
                db.expunge(row)
                return row
        await asyncio.sleep(0.1)
    async with db_session.session_scope() as db:
        row = await db.get(Message, message_id)
        cur = row.status if row else "<gone>"
    raise AssertionError(f"message {message_id} did not reach {status!r} (currently {cur!r})")


async def _only_message_id() -> int:
    async with db_session.session_scope() as db:
        ids = (await db.execute(select(Message.id).order_by(Message.id))).scalars().all()
    assert ids, "no message was enqueued"
    return ids[-1]


@pytest.fixture
async def sinks():
    a = FakeProvider()
    b = FakeProvider()
    await a.start()
    await b.start()
    yield a, b
    await a.stop()
    await b.stop()


async def _app(tmp_path_dir, sinks, **overrides):
    a, b = sinks
    primary_limits = overrides.pop("primary_limits", {})
    breaker_overrides = overrides.pop("breaker", {})
    providers = [
        provider("primary", a.port, priority=10, **primary_limits),
        provider("secondary", b.port, priority=20),
    ]
    settings = make_settings(tmp_path_dir, providers)
    settings.dispatch.spool_dir.mkdir(parents=True, exist_ok=True)
    for k, v in overrides.items():
        setattr(settings.dispatch.retry, k, v)
    for k, v in breaker_overrides.items():
        setattr(settings.circuit_breaker, k, v)
    app = Application(settings, with_web=False)
    await app.start()
    return app


async def test_basic_delivery_to_primary(tmp_path_dir, sinks):
    a, b = sinks
    app = await _app(tmp_path_dir, sinks)
    try:
        _, response = await _send(app.ctx.ingress.port)
        assert "queued as" in response
        mid = await _only_message_id()
        row = await _wait_status(mid, MessageStatus.SENT)
        assert row.provider_used == "primary"
        assert a.count == 1 and b.count == 0
    finally:
        await app.stop()


async def test_failover_on_transient_reject(tmp_path_dir, sinks):
    a, b = sinks
    a.mode = "reject"
    a.reject_code = 451
    app = await _app(tmp_path_dir, sinks)
    try:
        await _send(app.ctx.ingress.port)
        mid = await _only_message_id()
        row = await _wait_status(mid, MessageStatus.SENT)
        assert row.provider_used == "secondary"
        assert b.count == 1
        async with db_session.session_scope() as db:
            attempts = (
                await db.execute(
                    select(DeliveryAttempt).where(DeliveryAttempt.message_id == mid)
                    .order_by(DeliveryAttempt.attempt_no)
                )
            ).scalars().all()
        providers_tried = [x.provider for x in attempts]
        assert providers_tried == ["primary", "secondary"]
        assert attempts[0].result == "transient"
        assert attempts[1].result == "sent"
    finally:
        await app.stop()


async def test_rate_cap_routes_to_secondary(tmp_path_dir, sinks):
    a, b = sinks
    app = await _app(tmp_path_dir, sinks, primary_limits={"per_minute": 1})
    try:
        await _send(app.ctx.ingress.port, subject="m1")
        mid1 = await _only_message_id()
        await _wait_status(mid1, MessageStatus.SENT)

        await _send(app.ctx.ingress.port, subject="m2")
        mid2 = await _only_message_id()
        assert mid2 != mid1
        row2 = await _wait_status(mid2, MessageStatus.SENT)
        assert row2.provider_used == "secondary"
        assert a.count == 1 and b.count == 1
    finally:
        await app.stop()


async def test_holds_in_queue_until_a_provider_recovers(tmp_path_dir, sinks):
    a, b = sinks
    a.mode = b.mode = "drop"  # both refuse the connection mid-DATA
    app = await _app(
        tmp_path_dir, sinks,
        base_delay_seconds=1, max_delay_seconds=2,
        breaker={"failure_threshold": 3, "cooldown_seconds": 2},
    )
    try:
        await _send(app.ctx.ingress.port)
        mid = await _only_message_id()
        # Give the dispatcher a few cycles; it must NOT dead-letter.
        await asyncio.sleep(2)
        async with db_session.session_scope() as db:
            row = await db.get(Message, mid)
            assert row.status in (MessageStatus.QUEUED, MessageStatus.SENDING)

        a.mode = "accept"
        row = await _wait_status(mid, MessageStatus.SENT, timeout=20)
        assert row.provider_used == "primary"
    finally:
        await app.stop()


async def test_permanent_rejection_everywhere_deadletters(tmp_path_dir, sinks):
    a, b = sinks
    a.mode = b.mode = "reject"
    a.reject_code = b.reject_code = 550
    a.reject_message = b.reject_message = "5.1.1 no such user"
    app = await _app(tmp_path_dir, sinks)
    try:
        await _send(app.ctx.ingress.port)
        mid = await _only_message_id()
        row = await _wait_status(mid, MessageStatus.DEADLETTER)
        assert "rejected" in (row.last_error or "")
    finally:
        await app.stop()
