"""Admin CLI: ``python -m app.cli create-admin --username admin``.

Passwords are never accepted as arguments (they would land in shell history and ``ps``):
use the ``ZAI_ADMIN_PASSWORD`` environment variable or the hidden interactive prompt.
"""

import argparse
import asyncio
import getpass
import os
import sys

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.services import auth, users

PASSWORD_ENV = "ZAI_ADMIN_PASSWORD"  # noqa: S105 - name of an env var, not a secret


def _read_password() -> str:
    if password := os.environ.get(PASSWORD_ENV):
        return password
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        raise users.UserError("Passwords do not match")
    return password


async def _with_session(
    settings: Settings, action: str, username: str | None, password: str
) -> int:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            if action == "create-admin" and username:
                await users.create_user(session, username, password)
            elif action == "set-password" and username:
                await users.set_password(session, username, password)
            else:
                return await auth.purge_expired_sessions(session)
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create-admin", "create the administrator account"),
        ("set-password", "reset a password and revoke all of that user's sessions"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--username", required=True)
    sub.add_parser("purge-sessions", help="delete expired sessions")
    args = parser.parse_args(argv)
    settings = settings or get_settings()

    try:
        if args.command == "purge-sessions":
            purged = asyncio.run(_with_session(settings, "purge-sessions", None, ""))
            print(f"Purged {purged} expired session(s).")
            return 0
        password = _read_password()
        asyncio.run(_with_session(settings, args.command, args.username, password))
    except users.UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Done: {args.command} {args.username}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
