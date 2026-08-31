"""A minimal in-process SMTP sink used as a stand-in upstream provider in tests.

It can be told to accept, to reject with a chosen code, or to drop the
connection, and it records every message it accepts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from aiosmtpd.controller import UnthreadedController
from aiosmtpd.smtp import SMTP, Envelope, Session


@dataclass
class ReceivedMessage:
    mail_from: str
    rcpt_tos: list[str]
    data: bytes


@dataclass
class FakeProvider:
    host: str = "127.0.0.1"
    port: int = 0
    # Behaviour knobs (mutate at runtime from the test).
    mode: str = "accept"           # "accept" | "reject" | "drop"
    reject_code: int = 451
    reject_message: str = "4.7.0 Simulated temporary failure"
    received: list[ReceivedMessage] = field(default_factory=list)
    _controller: UnthreadedController | None = field(default=None, repr=False)

    async def start(self) -> None:
        handler = _Handler(self)
        self._controller = UnthreadedController(
            handler, hostname=self.host, port=self.port,
            loop=asyncio.get_running_loop(),
        )
        self._controller.server_coro = self._controller._create_server()
        self._controller.server = await self._controller.server_coro
        self.port = self._controller.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._controller and self._controller.server:
            await self._controller.finalize()

    @property
    def count(self) -> int:
        return len(self.received)


class _Handler:
    def __init__(self, parent: FakeProvider) -> None:
        self.parent = parent

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        mode = self.parent.mode
        if mode == "drop":
            raise ConnectionResetError("simulated disconnect")
        if mode == "reject":
            return f"{self.parent.reject_code} {self.parent.reject_message}"
        self.parent.received.append(
            ReceivedMessage(
                mail_from=envelope.mail_from or "",
                rcpt_tos=list(envelope.rcpt_tos),
                data=envelope.original_content or envelope.content or b"",
            )
        )
        return "250 2.0.0 OK: queued as fake"
