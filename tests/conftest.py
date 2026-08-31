"""Shared fixtures: an isolated settings object + initialised in-memory-ish DB."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest_asyncio

from smtp_switch.config import (
    DispatchConfig,
    IngressConfig,
    ProviderConfig,
    ProviderLimits,
    ProviderSMTP,
    Settings,
)
from smtp_switch.db import session as db_session


def make_settings(tmp: Path, providers: list[ProviderConfig] | None = None) -> Settings:
    return Settings(
        ingress=IngressConfig(
            host="127.0.0.1",
            port=0,
            require_auth=False,
            allowed_ips=["127.0.0.1/32", "::1/128"],
        ),
        dispatch=DispatchConfig(
            spool_dir=tmp / "spool",
            workers=2,
            poll_interval_seconds=0.1,
            no_capacity_backoff_seconds=1,
        ),
        providers=providers or [],
        database={"url": f"sqlite+aiosqlite:///{tmp}/switch.db"},
        logging={"level": "WARNING", "json": True},
    )


def provider(
    name: str, port: int, *, priority: int = 100, **limit_kwargs
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        priority=priority,
        smtp=ProviderSMTP(host="127.0.0.1", port=port, starttls=False, verify_cert=False),
        limits=ProviderLimits(**limit_kwargs),
    )


@pytest_asyncio.fixture
async def tmp_path_dir():
    d = Path(tempfile.mkdtemp(prefix="smtpswitch-test-"))
    yield d


@pytest_asyncio.fixture
async def settings(tmp_path_dir):
    s = make_settings(tmp_path_dir)
    s.dispatch.spool_dir.mkdir(parents=True, exist_ok=True)
    yield s


@pytest_asyncio.fixture
async def db(settings):
    db_session.init_engine(settings)
    await db_session.create_all()
    yield db_session
    await db_session.dispose_engine()
