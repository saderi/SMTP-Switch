"""FastAPI application factory for the dashboard + REST API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from smtp_switch.db.session import ping
from smtp_switch.logging_setup import get_logger
from smtp_switch.runtime import RuntimeContext
from smtp_switch.web.security import SessionManager, ensure_bootstrap_user

log = get_logger("web.app")

_HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await ensure_bootstrap_user()
    yield


def create_app(ctx: RuntimeContext) -> FastAPI:
    app = FastAPI(
        title="smtp-switch",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )
    app.state.ctx = ctx
    app.state.sessions = SessionManager(ctx.settings.web)
    app.state.templates = TEMPLATES

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    from smtp_switch.web import api, views

    app.include_router(views.router)
    app.include_router(api.router, prefix="/api")

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        await ping()
        return "ok"

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        if not ctx.settings.metrics.enabled:
            return PlainTextResponse("metrics disabled", status_code=404)
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    return app
