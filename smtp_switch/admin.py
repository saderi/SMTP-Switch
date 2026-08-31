"""``smtp-switch-admin`` — manage sending accounts and dashboard users from the CLI.

Useful for headless bootstrap (containers, provisioning scripts) without opening
the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import select

from smtp_switch.config import load_settings
from smtp_switch.db import session as db_session
from smtp_switch.db.models import Account, DashboardUser
from smtp_switch.security import hash_password


def _resolve_password(value: str | None, generate: bool) -> tuple[str, bool]:
    if value:
        return value, False
    if generate:
        return secrets.token_urlsafe(18), True
    import getpass

    pw = getpass.getpass("Password: ")
    if len(pw) < 8:
        sys.exit("password must be at least 8 characters")
    return pw, False


async def _account_add(args) -> None:
    password, generated = _resolve_password(args.password, args.generate)
    async with db_session.session_scope() as db:
        existing = (
            await db.execute(select(Account).where(Account.username == args.username))
        ).scalar_one_or_none()
        if existing is not None:
            sys.exit(f"account {args.username!r} already exists")
        db.add(
            Account(
                username=args.username,
                password_hash=hash_password(password),
                description=args.description,
            )
        )
    print(f"created sending account {args.username!r}")
    if generated:
        print(f"generated password: {password}")


async def _account_list(_args) -> None:
    async with db_session.session_scope() as db:
        rows = (await db.execute(select(Account).order_by(Account.username))).scalars().all()
    if not rows:
        print("(no accounts)")
        return
    for a in rows:
        state = "enabled" if a.enabled else "disabled"
        last = a.last_used_at.isoformat() if a.last_used_at else "never"
        print(f"{a.username:<24} {state:<9} last-used={last}  {a.description or ''}")


async def _account_set_enabled(args, enabled: bool) -> None:
    async with db_session.session_scope() as db:
        acct = (
            await db.execute(select(Account).where(Account.username == args.username))
        ).scalar_one_or_none()
        if acct is None:
            sys.exit(f"no such account: {args.username!r}")
        acct.enabled = enabled
    print(f"{args.username!r} {'enabled' if enabled else 'disabled'}")


async def _account_passwd(args) -> None:
    password, generated = _resolve_password(args.password, args.generate)
    async with db_session.session_scope() as db:
        acct = (
            await db.execute(select(Account).where(Account.username == args.username))
        ).scalar_one_or_none()
        if acct is None:
            sys.exit(f"no such account: {args.username!r}")
        acct.password_hash = hash_password(password)
        acct.last_used_at = None
    print(f"password updated for {args.username!r}")
    if generated:
        print(f"generated password: {password}")


async def _user_add(args) -> None:
    password, generated = _resolve_password(args.password, args.generate)
    async with db_session.session_scope() as db:
        existing = (
            await db.execute(
                select(DashboardUser).where(DashboardUser.username == args.username)
            )
        ).scalar_one_or_none()
        if existing is not None:
            sys.exit(f"dashboard user {args.username!r} already exists")
        db.add(
            DashboardUser(username=args.username, password_hash=hash_password(password))
        )
    print(f"created dashboard user {args.username!r}")
    if generated:
        print(f"generated password: {password}")


async def _user_passwd(args) -> None:
    password, generated = _resolve_password(args.password, args.generate)
    async with db_session.session_scope() as db:
        user = (
            await db.execute(
                select(DashboardUser).where(DashboardUser.username == args.username)
            )
        ).scalar_one_or_none()
        if user is None:
            sys.exit(f"no such dashboard user: {args.username!r}")
        user.password_hash = hash_password(password)
    print(f"password updated for dashboard user {args.username!r}")
    if generated:
        print(f"generated password: {password}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smtp-switch-admin")
    p.add_argument("-c", "--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="group", required=True)

    def pw_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--password", help="set explicitly (else prompt)")
        sp.add_argument("--generate", action="store_true", help="generate a random password")

    acc = sub.add_parser("account", help="sending-service SMTP-AUTH accounts")
    acc_sub = acc.add_subparsers(dest="action", required=True)
    a_add = acc_sub.add_parser("add")
    a_add.add_argument("username")
    a_add.add_argument("--description")
    pw_flags(a_add)
    a_add.set_defaults(func=_account_add)
    acc_sub.add_parser("list").set_defaults(func=_account_list)
    a_en = acc_sub.add_parser("enable")
    a_en.add_argument("username")
    a_en.set_defaults(func=lambda args: _account_set_enabled(args, True))
    a_dis = acc_sub.add_parser("disable")
    a_dis.add_argument("username")
    a_dis.set_defaults(func=lambda args: _account_set_enabled(args, False))
    a_pw = acc_sub.add_parser("passwd")
    a_pw.add_argument("username")
    pw_flags(a_pw)
    a_pw.set_defaults(func=_account_passwd)

    usr = sub.add_parser("user", help="dashboard login accounts")
    usr_sub = usr.add_subparsers(dest="action", required=True)
    u_add = usr_sub.add_parser("add")
    u_add.add_argument("username")
    pw_flags(u_add)
    u_add.set_defaults(func=_user_add)
    u_pw = usr_sub.add_parser("passwd")
    u_pw.add_argument("username")
    pw_flags(u_pw)
    u_pw.set_defaults(func=_user_passwd)

    return p


async def _main_async(args) -> None:
    settings = load_settings(args.config)
    db_session.init_engine(settings)
    if settings.database.auto_create:
        await db_session.create_all()
    try:
        await args.func(args)
    finally:
        await db_session.dispose_engine()


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
