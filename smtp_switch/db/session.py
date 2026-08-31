"""Async engine / session factory.

SQLite is opened in WAL mode with a busy timeout so the ingress writer, the
dispatcher workers and the web app can share the file without tripping over
``database is locked``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from smtp_switch.config import Settings
from smtp_switch.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _ensure_sqlite_parent(url: str) -> None:
    """``sqlite`` won't create the directory for a file DB — do it ourselves."""
    m = re.match(r"sqlite(?:\+\w+)?:///(?!:memory:)(.+)$", url)
    if not m:
        return
    path = Path(m.group(1))
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def init_engine(settings: Settings) -> AsyncEngine:
    """Create the process-wide engine + sessionmaker (idempotent)."""
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine
    is_sqlite = settings.database.url.startswith("sqlite")
    if is_sqlite:
        _ensure_sqlite_parent(settings.database.url)
    kwargs: dict = {"echo": settings.database.echo, "future": True}
    if is_sqlite:
        kwargs["pool_pre_ping"] = True
    _engine = create_async_engine(settings.database.url, **kwargs)
    if is_sqlite:
        _apply_sqlite_pragmas(_engine)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("engine not initialised; call init_engine(settings) first")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("sessionmaker not initialised; call init_engine(settings) first")
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transactional session: commit on success, rollback on error."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create any missing tables (used for tests and first-run bootstrap)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ping() -> bool:
    async with session_scope() as session:
        await session.execute(text("SELECT 1"))
    return True


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
