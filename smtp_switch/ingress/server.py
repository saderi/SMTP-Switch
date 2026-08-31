"""The ingress SMTP server: accept mail from internal services and enqueue it."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import uuid
from collections.abc import Callable
from email.parser import BytesHeaderParser
from email.utils import parseaddr
from pathlib import Path

from aiosmtpd.controller import UnthreadedController
from aiosmtpd.smtp import SMTP as SMTPServer
from aiosmtpd.smtp import Envelope, Session

from smtp_switch.config import Settings
from smtp_switch.db.models import Message, MessageStatus
from smtp_switch.db.session import session_scope
from smtp_switch.ingress.auth import AccountStore, peer_allowed
from smtp_switch.logging_setup import get_logger
from smtp_switch.metrics import MESSAGES_RECEIVED
from smtp_switch.util import utcnow

log = get_logger("ingress.server")

DEFAULT_SMTP_TIMEOUT = 120
_HEADER_PARSER = BytesHeaderParser()


class SpoolWriter:
    """Persist raw RFC822 bytes to ``<spool_dir>/<yyyy>/<mm>/<dd>/<uuid>.eml``."""

    def __init__(self, spool_dir: Path) -> None:
        self.root = spool_dir

    def write(self, content: bytes) -> tuple[str, int]:
        now = utcnow()
        subdir = self.root / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{uuid.uuid4().hex}.eml"
        path.write_bytes(content)
        return str(path), len(content)

    def read(self, spool_path: str) -> bytes:
        return Path(spool_path).read_bytes()

    def delete(self, spool_path: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            Path(spool_path).unlink()


class SwitchHandler:
    """aiosmtpd handler. One instance is shared across all connections."""

    def __init__(
        self,
        settings: Settings,
        spool: SpoolWriter,
        on_enqueue: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.spool = spool
        self._allowed_networks = settings.ingress.allowed_networks
        self._on_enqueue = on_enqueue

    # --- connection gating ---------------------------------------------------
    def _reject_peer(self, session: Session) -> str | None:
        if peer_allowed(session.peer, self._allowed_networks):
            return None
        log.warning("peer_rejected", peer=session.peer[0] if session.peer else None)
        return "550 5.7.1 Access denied"

    async def handle_EHLO(
        self, server: SMTPServer, session: Session, envelope: Envelope,
        hostname: str, responses: list[str],
    ) -> list[str]:
        session.host_name = hostname
        rejection = self._reject_peer(session)
        if rejection:
            return [rejection]
        return responses

    async def handle_HELO(
        self, server: SMTPServer, session: Session, envelope: Envelope, hostname: str,
    ) -> str:
        session.host_name = hostname
        rejection = self._reject_peer(session)
        if rejection:
            return rejection
        return f"250 {server.hostname}"

    # --- envelope ----------------------------------------------------------
    async def handle_MAIL(
        self, server: SMTPServer, session: Session, envelope: Envelope,
        address: str, mail_options: list[str],
    ) -> str:
        if self.settings.ingress.require_auth and not session.authenticated:
            return "530 5.7.0 Authentication required"
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(
        self, server: SMTPServer, session: Session, envelope: Envelope,
        address: str, rcpt_options: list[str],
    ) -> str:
        _, addr = parseaddr(address)
        if "@" not in addr:
            return "550 5.1.3 Bad recipient address syntax"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(
        self, server: SMTPServer, session: Session, envelope: Envelope,
    ) -> str:
        raw = envelope.original_content or envelope.content or b""
        content: bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8", "surrogateescape")
        if not envelope.rcpt_tos:
            return "554 5.5.1 No valid recipients"

        max_bytes = self.settings.ingress.max_message_bytes
        if len(content) > max_bytes:
            return f"552 5.3.4 Message exceeds size limit ({max_bytes} bytes)"

        account = session.auth_data if isinstance(session.auth_data, str) else None
        headers = _HEADER_PARSER.parsebytes(content)
        subject = (headers.get("Subject") or "").strip()[:512] or None
        msg_id = (headers.get("Message-ID") or "").strip()[:512] or None

        spool_path, size = self.spool.write(content)
        now = utcnow()
        try:
            async with session_scope() as db:
                row = Message(
                    received_at=now,
                    submitting_account=account,
                    from_addr=envelope.mail_from or "",
                    rcpt_to=list(envelope.rcpt_tos),
                    subject=subject,
                    message_id_header=msg_id,
                    size=size,
                    spool_path=spool_path,
                    status=MessageStatus.QUEUED,
                    next_attempt_at=now,
                )
                db.add(row)
                await db.flush()
                message_pk = row.id
        except Exception as exc:
            self.spool.delete(spool_path)
            log.error("enqueue_failed", error=str(exc))
            return "451 4.3.0 Temporary failure storing message"

        MESSAGES_RECEIVED.labels(account=account or "-").inc()
        if self._on_enqueue is not None:
            with contextlib.suppress(Exception):
                self._on_enqueue()
        log.info(
            "message_accepted",
            id=message_pk,
            account=account,
            mail_from=envelope.mail_from,
            rcpt_count=len(envelope.rcpt_tos),
            size=size,
        )
        return f"250 2.0.0 Message accepted for delivery (queued as {message_pk})"


class _GatedController(UnthreadedController):
    """UnthreadedController driven from an already-running event loop.

    ``Controller.hostname`` is the *bind* address; the SMTP greeting hostname is
    a separate concern, injected here via :meth:`factory`.
    """

    def __init__(self, *args, greeting_hostname: str | None = None, **kwargs) -> None:
        self._greeting_hostname = greeting_hostname
        super().__init__(*args, **kwargs)

    def factory(self):  # noqa: D102 - see base class
        return SMTPServer(
            self.handler,
            hostname=self._greeting_hostname,
            **self.SMTP_kwargs,
        )

    async def start_async(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.server_coro = self._create_server()
        self.server = await self.server_coro

    async def stop_async(self) -> None:
        if self.server is not None:
            await self.finalize()


def _build_tls_context(settings: Settings) -> ssl.SSLContext | None:
    tls = settings.ingress.tls
    if not tls.enabled:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(tls.cert_file), keyfile=str(tls.key_file))
    return ctx


class IngressServer:
    """Owns the aiosmtpd controller and the account cache refresh loop."""

    def __init__(
        self, settings: Settings, on_enqueue: Callable[[], None] | None = None
    ) -> None:
        self.settings = settings
        self.spool = SpoolWriter(settings.dispatch.spool_dir)
        self.accounts = AccountStore()
        self.handler = SwitchHandler(settings, self.spool, on_enqueue=on_enqueue)
        self._controller: _GatedController | None = None
        self._refresh_task: asyncio.Task | None = None

    @property
    def port(self) -> int:
        server = self._controller.server if self._controller else None
        sockets = getattr(server, "sockets", None)
        if sockets:
            return int(sockets[0].getsockname()[1])
        return self.settings.ingress.port

    async def start(self) -> None:
        self.settings.dispatch.spool_dir.mkdir(parents=True, exist_ok=True)
        await self.accounts.refresh()

        tls_context = _build_tls_context(self.settings)
        ing = self.settings.ingress
        smtp_kwargs = {
            "data_size_limit": ing.max_message_bytes,
            "enable_SMTPUTF8": True,
            "ident": "smtp-switch",
            "timeout": DEFAULT_SMTP_TIMEOUT,
            "tls_context": tls_context,
            "require_starttls": ing.tls.require_starttls,
            "auth_required": ing.require_auth,
            "auth_require_tls": bool(tls_context) and ing.tls.require_starttls,
            "authenticator": self.accounts.make_authenticator(),
        }
        self._controller = _GatedController(
            self.handler, hostname=ing.host, port=ing.port,
            greeting_hostname=ing.hostname,
            loop=asyncio.get_running_loop(), **smtp_kwargs,
        )
        await self._controller.start_async()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        log.info(
            "ingress_started",
            host=ing.host, port=ing.port,
            tls=bool(tls_context), auth_required=ing.require_auth,
            allowed_ips=ing.allowed_ips,
        )

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self.accounts.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                log.warning("account_refresh_failed", error=str(exc))

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
        if self._controller:
            await self._controller.stop_async()
        log.info("ingress_stopped")
