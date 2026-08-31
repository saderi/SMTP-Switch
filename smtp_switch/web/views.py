"""Server-rendered dashboard pages. Mutations are done by app.js against /api."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from smtp_switch.web import queries
from smtp_switch.web.security import optional_user, require_user

router = APIRouter()


def _tpl(request: Request):
    return request.app.state.templates


def _ctx(request: Request):
    return request.app.state.ctx


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: str | None = Depends(optional_user)):
    if user:
        return RedirectResponse("/", status_code=302)
    return _tpl(request).TemplateResponse(request, "login.html", {"title": "Sign in"})


@router.get("/", response_class=HTMLResponse)
async def overview_page(request: Request, user: str = Depends(require_user)):
    ctx = _ctx(request)
    return _tpl(request).TemplateResponse(
        request,
        "overview.html",
        {
            "title": "Overview",
            "user": user,
            "queue": await queries.queue_counts(),
            "throughput": await queries.throughput(60),
            "providers": await queries.provider_overview(ctx),
        },
    )


@router.get("/messages", response_class=HTMLResponse)
async def messages_page(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    offset: int = 0,
    user: str = Depends(require_user),
):
    rows = await queries.list_messages(status=status, q=q, limit=50, offset=offset)
    return _tpl(request).TemplateResponse(
        request,
        "messages.html",
        {
            "title": "Messages",
            "user": user,
            "messages": rows,
            "status": status or "",
            "q": q or "",
            "offset": offset,
            "next_offset": offset + 50 if len(rows) == 50 else None,
            "prev_offset": max(0, offset - 50) if offset else None,
        },
    )


@router.get("/messages/{message_id}", response_class=HTMLResponse)
async def message_page(request: Request, message_id: int, user: str = Depends(require_user)):
    found = await queries.get_message(message_id)
    if found is None:
        return _tpl(request).TemplateResponse(
            request, "not_found.html", {"title": "Not found", "user": user}, status_code=404
        )
    msg, attempts = found
    return _tpl(request).TemplateResponse(
        request,
        "message_detail.html",
        {"title": f"Message {message_id}", "user": user, "msg": msg, "attempts": attempts},
    )


@router.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request, user: str = Depends(require_user)):
    return _tpl(request).TemplateResponse(
        request,
        "providers.html",
        {
            "title": "Providers",
            "user": user,
            "providers": await queries.provider_overview(_ctx(request)),
        },
    )


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, user: str = Depends(require_user)):
    from sqlalchemy import select

    from smtp_switch.db.models import Account
    from smtp_switch.db.session import session_scope

    async with session_scope() as db:
        accounts = list(
            (await db.execute(select(Account).order_by(Account.username))).scalars().all()
        )
    return _tpl(request).TemplateResponse(
        request,
        "accounts.html",
        {"title": "Accounts", "user": user, "accounts": accounts},
    )
