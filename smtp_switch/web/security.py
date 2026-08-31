"""Session-cookie auth for the dashboard.

A signed (itsdangerous) cookie carries the username and an issued-at stamp.
Credentials live in the ``dashboard_users`` table. On first run, if that table
is empty, a one-time ``admin`` account is created and its generated password is
printed to the log.
"""

from __future__ import annotations

import secrets

from fastapi import Cookie, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select

from smtp_switch.config import WebConfig
from smtp_switch.db.models import DashboardUser
from smtp_switch.db.session import session_scope
from smtp_switch.logging_setup import get_logger
from smtp_switch.security import hash_password, verify_password

log = get_logger("web.security")

COOKIE_NAME = "smtp_switch_session"


class SessionManager:
    def __init__(self, cfg: WebConfig) -> None:
        self._cfg = cfg
        self._serializer = URLSafeTimedSerializer(cfg.session_secret, salt="dashboard")
        self.ttl = cfg.session_ttl_seconds

    def issue(self, username: str) -> str:
        return self._serializer.dumps({"u": username})

    def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self.ttl)
        except (BadSignature, SignatureExpired):
            return None
        return data.get("u")


async def ensure_bootstrap_user() -> None:
    async with session_scope() as db:
        count = (await db.execute(select(func.count()).select_from(DashboardUser))).scalar_one()
        if count:
            return
        password = secrets.token_urlsafe(18)
        db.add(DashboardUser(username="admin", password_hash=hash_password(password)))
    log.warning(
        "dashboard_bootstrap_user_created",
        username="admin",
        password=password,
        hint="Log in and change this immediately; it will not be shown again.",
    )


async def authenticate(username: str, password: str) -> str | None:
    async with session_scope() as db:
        user = (
            await db.execute(select(DashboardUser).where(DashboardUser.username == username))
        ).scalar_one_or_none()
        if user is None or not user.enabled:
            return None
        if not verify_password(user.password_hash, password):
            return None
        return user.username


def get_session_manager(request: Request) -> SessionManager:
    return request.app.state.sessions


async def require_user(
    request: Request,
    smtp_switch_session: str | None = Cookie(default=None),
) -> str:
    username = get_session_manager(request).verify(smtp_switch_session)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"Location": "/login"},
        )
    return username


async def optional_user(
    request: Request,
    smtp_switch_session: str | None = Cookie(default=None),
) -> str | None:
    return get_session_manager(request).verify(smtp_switch_session)
