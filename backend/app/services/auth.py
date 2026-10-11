"""Login, sessions and password change (T5.1, T5.3)."""

import ipaddress
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserSession
from app.security.passwords import (
    MIN_PASSWORD_LENGTH,
    dummy_hash,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.security.tokens import SESSION_TTL_SECONDS, hash_token, new_token

LAST_SEEN_GRANULARITY = timedelta(seconds=60)  # avoid a write on every request


@dataclass(frozen=True)
class AdminIdentity:
    user_id: int
    username: str
    session_id: int


class WrongCurrentPassword(Exception):
    pass


class PasswordRuleViolation(Exception):
    """The new password is unacceptable; the message is fixed text and safe to return."""


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    """Return the user for valid credentials, else ``None``.

    Unknown usernames still verify against a dummy hash so timing does not reveal them.
    """
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    valid = await verify_password(user.password_hash if user else dummy_hash(), password)
    if user is None or not valid:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password(password)
        await session.commit()
    return user


def _client_ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address(value)) if value else None
    except ValueError:
        return None


async def create_session(
    session: AsyncSession, user: User, ip: str | None, user_agent: str | None
) -> str:
    """Create a session and return the bearer token (only its hash is stored)."""
    token = new_token()
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=func.now() + timedelta(seconds=SESSION_TTL_SECONDS),
            ip=_client_ip(ip),
            user_agent=user_agent[:255] if user_agent else None,
        )
    )
    await session.commit()
    return token


async def find_session(session: AsyncSession, token: str) -> AdminIdentity | None:
    stale = UserSession.last_seen_at < func.now() - LAST_SEEN_GRANULARITY
    row = (
        await session.execute(
            select(UserSession.id, UserSession.user_id, User.username, stale)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(token), UserSession.expires_at > func.now())
        )
    ).first()
    if row is None:
        return None
    session_id, user_id, username, is_stale = row
    if is_stale:
        await session.execute(
            update(UserSession).where(UserSession.id == session_id).values(last_seen_at=func.now())
        )
        await session.commit()
    return AdminIdentity(user_id=user_id, username=username, session_id=session_id)


async def delete_session(session: AsyncSession, session_id: int) -> None:
    await session.execute(delete(UserSession).where(UserSession.id == session_id))
    await session.commit()


async def change_password(
    session: AsyncSession, identity: AdminIdentity, current_password: str, new_password: str
) -> None:
    """Check the current password (403), the new one (422), then revoke all *other* sessions."""
    user = (
        await session.execute(select(User).where(User.id == identity.user_id).with_for_update())
    ).scalar_one()
    if not await verify_password(user.password_hash, current_password):
        raise WrongCurrentPassword
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise PasswordRuleViolation(
            f"The new password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if new_password == current_password:
        raise PasswordRuleViolation("The new password must differ from the current one.")
    user.password_hash = await hash_password(new_password)
    user.password_changed_at = func.now()
    await session.execute(
        delete(UserSession).where(
            UserSession.user_id == user.id, UserSession.id != identity.session_id
        )
    )
    await session.commit()


async def purge_expired_sessions(session: AsyncSession) -> int:
    result = await session.execute(delete(UserSession).where(UserSession.expires_at <= func.now()))
    await session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
