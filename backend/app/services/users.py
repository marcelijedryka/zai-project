"""User management (used by the CLI and by tests)."""

import re

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserSession
from app.security.passwords import MIN_PASSWORD_LENGTH, hash_password

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._@-]{3,50}")


class UserError(ValueError):
    """Invalid input for a user operation (message is safe to show)."""


class UsernameTaken(UserError):
    pass


class UserNotFound(UserError):
    pass


def _validate(username: str | None, password: str) -> None:
    if username is not None and not USERNAME_PATTERN.fullmatch(username):
        raise UserError("Username must be 3-50 characters of letters, digits and . _ @ -")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")


async def create_user(session: AsyncSession, username: str, password: str) -> User:
    _validate(username, password)
    user = User(username=username, password_hash=await hash_password(password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise UsernameTaken(f"User {username!r} already exists") from exc
    return user


async def set_password(session: AsyncSession, username: str, password: str) -> None:
    """Replace a user's password and revoke every session of that user."""
    _validate(None, password)
    result = await session.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFound(f"User {username!r} does not exist")
    user.password_hash = await hash_password(password)
    await session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    await session.commit()
