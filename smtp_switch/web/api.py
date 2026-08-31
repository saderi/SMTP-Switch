"""JSON API. Everything except /login and /logout requires a valid session."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from smtp_switch.db.models import Account, Message, MessageStatus
from smtp_switch.db.session import session_scope
from smtp_switch.ingress.server import SpoolWriter
from smtp_switch.logging_setup import get_logger
from smtp_switch.security import hash_password
from smtp_switch.util import utcnow
from smtp_switch.web import queries
from smtp_switch.web.security import COOKIE_NAME, authenticate, require_user

log = get_logger("web.api")
router = APIRouter()


def _ctx(request: Request):
    return request.app.state.ctx


# --------------------------------------------------------------------- auth
class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(request: Request, body: LoginBody) -> JSONResponse:
    username = await authenticate(body.username, body.password)
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = request.app.state.sessions.issue(username)
    resp = JSONResponse({"ok": True, "username": username})
    resp.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=request.app.state.sessions.ttl,
    )
    return resp


@router.post("/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ----------------------------------------------------------------- overview
@router.get("/overview")
async def overview(request: Request, user: str = Depends(require_user)) -> dict:
    ctx = _ctx(request)
    return {
        "queue": await queries.queue_counts(),
        "throughput": await queries.throughput(60),
        "providers": await queries.provider_overview(ctx),
    }


# ----------------------------------------------------------------- messages
@router.get("/messages")
async def messages(
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: str = Depends(require_user),
) -> dict:
    rows = await queries.list_messages(
        status=status, q=q, limit=min(limit, 200), offset=offset
    )
    return {"messages": [_msg_dict(m) for m in rows]}


@router.get("/messages/{message_id}")
async def message_detail(message_id: int, user: str = Depends(require_user)) -> dict:
    found = await queries.get_message(message_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    msg, attempts = found
    return {
        "message": _msg_dict(msg),
        "attempts": [
            {
                "attempt_no": a.attempt_no,
                "provider": a.provider,
                "result": a.result,
                "smtp_code": a.smtp_code,
                "error": a.error,
                "started_at": _iso(a.started_at),
                "finished_at": _iso(a.finished_at),
            }
            for a in attempts
        ],
    }


@router.get("/messages/{message_id}/raw")
async def message_raw(
    request: Request, message_id: int, user: str = Depends(require_user)
) -> Response:
    found = await queries.get_message(message_id)
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    msg, _ = found
    spool = SpoolWriter(_ctx(request).settings.dispatch.spool_dir)
    try:
        raw = spool.read(msg.spool_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=410, detail="spooled body no longer on disk"
        ) from None
    return Response(
        content=raw,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="message-{message_id}.eml"'},
    )


@router.post("/messages/{message_id}/requeue")
async def requeue(
    request: Request, message_id: int, user: str = Depends(require_user)
) -> dict:
    async with session_scope() as db:
        msg = await db.get(Message, message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="not found")
        if msg.status not in (MessageStatus.DEADLETTER, MessageStatus.SENT):
            raise HTTPException(status_code=409, detail=f"cannot requeue from {msg.status}")
        msg.status = MessageStatus.QUEUED
        msg.attempts = 0
        msg.next_attempt_at = utcnow()
        msg.last_error = None
        msg.claimed_at = None
    _ctx(request).dispatcher.notify_new_message()
    log.info("message_requeued_via_api", message_id=message_id, by=user)
    return {"ok": True}


@router.delete("/messages/{message_id}")
async def delete_message(
    request: Request, message_id: int, user: str = Depends(require_user)
) -> dict:
    async with session_scope() as db:
        msg = await db.get(Message, message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="not found")
        spool_path = msg.spool_path
        await db.delete(msg)
    SpoolWriter(_ctx(request).settings.dispatch.spool_dir).delete(spool_path)
    log.info("message_deleted_via_api", message_id=message_id, by=user)
    return {"ok": True}


# ---------------------------------------------------------------- providers
@router.post("/providers/{name}/enable")
async def enable_provider(
    request: Request, name: str, user: str = Depends(require_user)
) -> dict:
    return await _set_provider_enabled(request, name, True, user)


@router.post("/providers/{name}/disable")
async def disable_provider(
    request: Request, name: str, user: str = Depends(require_user)
) -> dict:
    return await _set_provider_enabled(request, name, False, user)


async def _set_provider_enabled(request: Request, name: str, enabled: bool, user: str) -> dict:
    ctx = _ctx(request)
    try:
        await ctx.registry.set_enabled(name, enabled, note=f"set by {user}")
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown provider") from None
    return {"ok": True, "provider": name, "enabled": enabled}


@router.post("/providers/{name}/reset-breaker")
async def reset_breaker(
    request: Request, name: str, user: str = Depends(require_user)
) -> dict:
    ctx = _ctx(request)
    if ctx.registry.get(name) is None:
        raise HTTPException(status_code=404, detail="unknown provider")
    await ctx.breaker.record_success(name)
    log.info("breaker_reset_via_api", provider=name, by=user)
    return {"ok": True}


# ----------------------------------------------------------------- accounts
class AccountBody(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8)
    description: str | None = None


@router.get("/accounts")
async def list_accounts(request: Request, user: str = Depends(require_user)) -> dict:
    async with session_scope() as db:
        rows = (await db.execute(select(Account).order_by(Account.username))).scalars().all()
        return {
            "accounts": [
                {
                    "id": a.id,
                    "username": a.username,
                    "description": a.description,
                    "enabled": a.enabled,
                    "created_at": _iso(a.created_at),
                    "last_used_at": _iso(a.last_used_at),
                }
                for a in rows
            ]
        }


@router.post("/accounts")
async def create_account(
    request: Request, body: AccountBody, user: str = Depends(require_user)
) -> dict:
    async with session_scope() as db:
        exists = (
            await db.execute(select(Account).where(Account.username == body.username))
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=409, detail="username already exists")
        db.add(
            Account(
                username=body.username,
                password_hash=hash_password(body.password),
                description=body.description,
            )
        )
    await _refresh_accounts(request)
    log.info("account_created_via_api", username=body.username, by=user)
    return {"ok": True}


@router.post("/accounts/{account_id}/password")
async def reset_account_password(
    request: Request, account_id: int, body: dict, user: str = Depends(require_user)
) -> dict:
    password = str(body.get("password", ""))
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="password too short (min 8)")
    async with session_scope() as db:
        acct = await db.get(Account, account_id)
        if acct is None:
            raise HTTPException(status_code=404, detail="not found")
        acct.password_hash = hash_password(password)
    await _refresh_accounts(request)
    return {"ok": True}


@router.post("/accounts/{account_id}/toggle")
async def toggle_account(
    request: Request, account_id: int, user: str = Depends(require_user)
) -> dict:
    async with session_scope() as db:
        acct = await db.get(Account, account_id)
        if acct is None:
            raise HTTPException(status_code=404, detail="not found")
        acct.enabled = not acct.enabled
        new_state = acct.enabled
    await _refresh_accounts(request)
    return {"ok": True, "enabled": new_state}


@router.delete("/accounts/{account_id}")
async def delete_account(
    request: Request, account_id: int, user: str = Depends(require_user)
) -> dict:
    async with session_scope() as db:
        await db.execute(delete(Account).where(Account.id == account_id))
    await _refresh_accounts(request)
    log.info("account_deleted_via_api", account_id=account_id, by=user)
    return {"ok": True}


async def _refresh_accounts(request: Request) -> None:
    ingress = _ctx(request).ingress
    if ingress is not None:
        await ingress.accounts.refresh()


# ------------------------------------------------------------------ helpers
def _iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _msg_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "status": m.status,
        "from_addr": m.from_addr,
        "rcpt_to": m.rcpt_to,
        "subject": m.subject,
        "message_id_header": m.message_id_header,
        "size": m.size,
        "attempts": m.attempts,
        "provider_used": m.provider_used,
        "submitting_account": m.submitting_account,
        "received_at": _iso(m.received_at),
        "next_attempt_at": _iso(m.next_attempt_at),
        "sent_at": _iso(m.sent_at),
        "last_error": m.last_error,
    }
