"""Dashboard + API smoke tests via an in-process ASGI client."""

from __future__ import annotations

import httpx
import pytest

from smtp_switch.db.models import DashboardUser
from smtp_switch.db.session import session_scope
from smtp_switch.main import Application
from smtp_switch.security import hash_password
from smtp_switch.web.app import create_app
from smtp_switch.web.security import ensure_bootstrap_user
from tests.conftest import make_settings, provider

pytestmark = pytest.mark.asyncio


async def _client(tmp_path_dir):
    settings = make_settings(tmp_path_dir, [provider("primary", 9999, priority=10)])
    settings.dispatch.spool_dir.mkdir(parents=True, exist_ok=True)
    app_obj = Application(settings, with_ingress=False, with_web=False)
    await app_obj.start()
    async with session_scope() as db:
        db.add(DashboardUser(username="tester", password_hash=hash_password("password123")))
    fastapi_app = create_app(app_obj.ctx)
    await ensure_bootstrap_user()  # no-op: user already exists
    transport = httpx.ASGITransport(app=fastapi_app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app_obj, client


async def test_requires_auth_then_logs_in(tmp_path_dir):
    app_obj, client = await _client(tmp_path_dir)
    try:
        r = await client.get("/api/overview")
        assert r.status_code == 401

        r = await client.post(
            "/api/login", json={"username": "tester", "password": "password123"}
        )
        assert r.status_code == 200
        assert client.cookies.get("smtp_switch_session")

        r = await client.get("/api/overview")
        assert r.status_code == 200
        body = r.json()
        assert "queue" in body and "providers" in body
        assert body["providers"][0]["name"] == "primary"
    finally:
        await client.aclose()
        await app_obj.stop()


async def test_healthz_and_metrics_are_public(tmp_path_dir):
    app_obj, client = await _client(tmp_path_dir)
    try:
        assert (await client.get("/healthz")).status_code == 200
        m = await client.get("/metrics")
        assert m.status_code == 200
        assert "smtp_switch_messages_received_total" in m.text
    finally:
        await client.aclose()
        await app_obj.stop()


async def test_account_crud_and_provider_toggle(tmp_path_dir):
    app_obj, client = await _client(tmp_path_dir)
    try:
        await client.post(
            "/api/login", json={"username": "tester", "password": "password123"}
        )
        r = await client.post(
            "/api/accounts",
            json={"username": "billing", "password": "supersecret", "description": "x"},
        )
        assert r.status_code == 200
        r = await client.get("/api/accounts")
        assert [a["username"] for a in r.json()["accounts"]] == ["billing"]

        r = await client.post("/api/providers/primary/disable")
        assert r.status_code == 200
        assert app_obj.ctx.registry.is_enabled("primary") is False

        r = await client.post("/api/providers/primary/enable")
        assert app_obj.ctx.registry.is_enabled("primary") is True

        r = await client.post("/api/providers/nope/disable")
        assert r.status_code == 404
    finally:
        await client.aclose()
        await app_obj.stop()
