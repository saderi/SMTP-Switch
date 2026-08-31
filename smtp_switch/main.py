"""Process entrypoint: wire ingress + dispatcher + web into one event loop."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal

import uvicorn

from smtp_switch.config import Settings, load_settings
from smtp_switch.db import session as db_session
from smtp_switch.dispatch.health import CircuitBreaker
from smtp_switch.dispatch.worker import Dispatcher
from smtp_switch.ingress.server import IngressServer
from smtp_switch.logging_setup import configure_logging, get_logger
from smtp_switch.providers.registry import ProviderRegistry
from smtp_switch.runtime import RuntimeContext

log = get_logger("main")


class Application:
    """Owns every long-lived subsystem; ``run()`` blocks until a signal."""

    def __init__(self, settings: Settings, *, with_ingress: bool = True, with_web: bool = True):
        self.settings = settings
        self._with_ingress = with_ingress
        self._with_web = with_web
        self.ctx: RuntimeContext | None = None
        self._web_server: uvicorn.Server | None = None
        self._web_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> RuntimeContext:
        configure_logging(self.settings.logging)
        db_session.init_engine(self.settings)
        if self.settings.database.auto_create:
            await db_session.create_all()

        registry = ProviderRegistry(self.settings)
        await registry.refresh_overrides()
        breaker = CircuitBreaker(self.settings.circuit_breaker)
        await breaker.load()
        dispatcher = Dispatcher(self.settings, registry, breaker)

        ingress = None
        if self._with_ingress:
            ingress = IngressServer(
                self.settings, on_enqueue=dispatcher.notify_new_message
            )

        self.ctx = RuntimeContext(
            settings=self.settings,
            registry=registry,
            breaker=breaker,
            dispatcher=dispatcher,
            ingress=ingress,
        )

        await dispatcher.start()
        if ingress is not None:
            await ingress.start()
        if self._with_web:
            await self._start_web()

        log.info("smtp_switch_started")
        return self.ctx

    async def _start_web(self) -> None:
        from smtp_switch.web.app import create_app

        assert self.ctx is not None
        app = create_app(self.ctx)
        config = uvicorn.Config(
            app,
            host=self.settings.web.host,
            port=self.settings.web.port,
            log_config=None,
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        # We install our own SIGINT/SIGTERM handlers in run(); stop uvicorn's.
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
        self._web_server = server
        self._web_task = asyncio.create_task(server.serve(), name="web")
        # Wait until uvicorn is actually accepting connections.
        while not self._web_server.started:
            await asyncio.sleep(0.02)
        log.info("web_started", host=self.settings.web.host, port=self.settings.web.port)

    async def stop(self) -> None:
        log.info("smtp_switch_stopping")
        if self._web_server is not None:
            self._web_server.should_exit = True
        if self._web_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._web_task
        if self.ctx and self.ctx.ingress is not None:
            await self.ctx.ingress.stop()
        if self.ctx is not None:
            await self.ctx.dispatcher.stop()
        await db_session.dispose_engine()
        log.info("smtp_switch_stopped")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.stop()


def run() -> None:
    parser = argparse.ArgumentParser(prog="smtp-switch")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("--no-web", action="store_true", help="don't start the dashboard")
    parser.add_argument(
        "--no-ingress", action="store_true", help="dispatcher/web only (no SMTP listener)"
    )
    args = parser.parse_args()

    settings = load_settings(args.config)
    app = Application(
        settings, with_ingress=not args.no_ingress, with_web=not args.no_web
    )
    asyncio.run(app.run())


if __name__ == "__main__":
    run()
