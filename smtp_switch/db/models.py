"""SQLAlchemy 2.0 models for the switch's state.

Message *bodies* are not stored here — they live as files under the spool dir,
referenced by :attr:`Message.spool_path`. Everything else (queue metadata,
rate-limit bookkeeping, provider health, credentials) is in these tables.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from smtp_switch.util import utcnow


class Base(DeclarativeBase):
    pass


class MessageStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DEADLETTER = "deadletter"


class AttemptResult(StrEnum):
    SENT = "sent"
    TRANSIENT = "transient"        # retry later (4xx / connection error)
    PERMANENT = "permanent"       # message rejected (5xx)
    NO_CAPACITY = "no_capacity"   # every provider was down or capped
    ERROR = "error"               # unexpected internal error


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    submitting_account: Mapped[str | None] = mapped_column(String(255))
    from_addr: Mapped[str] = mapped_column(String(320))
    rcpt_to: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject: Mapped[str | None] = mapped_column(String(512))
    message_id_header: Mapped[str | None] = mapped_column(String(512), index=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    spool_path: Mapped[str] = mapped_column(String(1024))

    status: Mapped[str] = mapped_column(String(16), default=MessageStatus.QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)

    provider_used: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)

    attempts_log: Mapped[list[DeliveryAttempt]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="DeliveryAttempt.attempt_no",
    )

    __table_args__ = (
        Index("ix_messages_status_next_attempt", "status", "next_attempt_at"),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[str] = mapped_column(String(16))
    smtp_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    message: Mapped[Message] = relationship(back_populates="attempts_log")


class SendLogEntry(Base):
    """One row per message handed to a provider — the sliding-window ledger."""

    __tablename__ = "send_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int | None] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_send_log_provider_sent_at", "provider", "sent_at"),
    )


class ProviderQuota(Base):
    """Day/month counters that a sliding window can't cheaply express."""

    __tablename__ = "provider_quota"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_key: Mapped[str] = mapped_column(String(16), primary_key=True)  # YYYY-MM or YYYY-MM-DD
    scope: Mapped[str] = mapped_column(String(8), primary_key=True)        # "day" | "month"
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProviderState(Base):
    """Persisted circuit-breaker state per provider."""

    __tablename__ = "provider_state"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)
    half_open_probes: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Account(Base):
    """A sending service's SMTP-AUTH credentials."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class DashboardUser(Base):
    """A login for the web dashboard."""

    __tablename__ = "dashboard_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProviderOverride(Base):
    """Operator toggles that outlive a restart (e.g. disable a provider from the UI)."""

    __tablename__ = "provider_overrides"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool | None] = mapped_column(Boolean)  # None => defer to config
    note: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


__all__ = [
    "Base",
    "MessageStatus",
    "AttemptResult",
    "Message",
    "DeliveryAttempt",
    "SendLogEntry",
    "ProviderQuota",
    "ProviderState",
    "Account",
    "DashboardUser",
    "ProviderOverride",
]
