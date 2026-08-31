"""SMTP-AUTH authenticator and source-IP allowlist for the ingress server.

aiosmtpd invokes the ``authenticator`` synchronously from inside the event loop,
so it cannot ``await``. We therefore keep an in-memory snapshot of the
``accounts`` table (:class:`AccountStore`), refreshed periodically and on demand
by the dashboard. The only cost paid on the loop thread per login is one argon2
``verify`` (a few milliseconds); side effects (``last_used_at``, hash upgrades)
are scheduled as fire-and-forget tasks.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Any

from aiosmtpd.smtp import AuthResult, LoginPassword
from sqlalchemy import select

from smtp_switch.db.models import Account
from smtp_switch.db.session import session_scope
from smtp_switch.logging_setup import get_logger
from smtp_switch.security import hash_password, needs_rehash, verify_password
from smtp_switch.util import utcnow

log = get_logger("ingress.auth")


def peer_allowed(peer: Any, networks: list) -> bool:
    """True if ``peer`` (aiosmtpd's ``(host, port, ...)`` tuple) is in an allowed network."""
    if not networks:
        return True
    if not peer or not isinstance(peer, (tuple, list)):
        return False
    try:
        addr = ipaddress.ip_address(peer[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in networks)


@dataclass(slots=True)
class _CachedAccount:
    id: int
    password_hash: str
    enabled: bool


class AccountStore:
    """Refreshable in-memory view of sending-service credentials."""

    def __init__(self) -> None:
        self._by_name: dict[str, _CachedAccount] = {}

    async def refresh(self) -> None:
        async with session_scope() as db:
            rows = (await db.execute(select(Account))).scalars().all()
            self._by_name = {
                r.username: _CachedAccount(r.id, r.password_hash, r.enabled) for r in rows
            }
        log.debug("account_store_refreshed", count=len(self._by_name))

    def verify(self, username: str, password: str) -> str | None:
        acct = self._by_name.get(username)
        if acct is None or not acct.enabled:
            return None
        if not verify_password(acct.password_hash, password):
            return None
        self._schedule_touch(acct.id, password if needs_rehash(acct.password_hash) else None)
        return username

    def _schedule_touch(self, account_id: int, password_to_rehash: str | None) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only in unit tests
            return
        loop.create_task(_touch_account(account_id, password_to_rehash))

    def make_authenticator(self):
        """Return the callable to pass to aiosmtpd as ``authenticator=``."""

        def _authenticator(server, session, envelope, mechanism, auth_data):  # noqa: ANN001
            if not isinstance(auth_data, LoginPassword):
                return AuthResult(success=False, handled=False)
            username = _decode(auth_data.login)
            password = _decode(auth_data.password)
            peer = session.peer[0] if getattr(session, "peer", None) else None
            if not username or not password:
                return AuthResult(success=False, handled=False)
            if self.verify(username, password) is None:
                log.info("auth_failed", username=username, peer=peer, mechanism=mechanism)
                return AuthResult(success=False, handled=False)
            log.info("auth_ok", username=username, peer=peer, mechanism=mechanism)
            return AuthResult(success=True, auth_data=username)

        return _authenticator


async def _touch_account(account_id: int, password_to_rehash: str | None) -> None:
    try:
        async with session_scope() as db:
            acct = await db.get(Account, account_id)
            if acct is None:
                return
            acct.last_used_at = utcnow()
            if password_to_rehash:
                acct.password_hash = hash_password(password_to_rehash)
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("account_touch_failed", account_id=account_id, error=str(exc))


def _decode(value) -> str:  # noqa: ANN001
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")
