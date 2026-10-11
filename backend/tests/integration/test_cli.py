import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import cli
from app.config import Settings
from app.security import passwords
from app.security.tokens import hash_token
from app.services import auth
from tests.conftest import Admin


async def _hash_of(engine: AsyncEngine, username: str) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT password_hash FROM users WHERE username = :u"), {"u": username}
            )
        ).scalar_one_or_none()


async def run(
    command: str, username: str, settings: Settings, monkeypatch: pytest.MonkeyPatch, password: str
) -> int:
    monkeypatch.setenv(cli.PASSWORD_ENV, password)
    # The CLI calls asyncio.run(), which needs a thread without a running event loop.
    return await asyncio.to_thread(cli.main, [command, "--username", username], settings)


async def test_create_admin_stores_an_argon2id_hash(
    settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = await run("create-admin", "root", settings, monkeypatch, "a long enough pw")
    assert code == 0
    stored = await _hash_of(engine, "root")
    assert stored is not None
    assert stored.startswith("$argon2id$")
    assert passwords.verify_password_sync(stored, "a long enough pw")


async def test_create_admin_rejects_duplicates_and_weak_input(
    settings: Settings,
    engine: AsyncEngine,
    admin: Admin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    duplicate = await run("create-admin", admin.username, settings, monkeypatch, "pw12345!")
    assert duplicate == 1
    assert "already exists" in capsys.readouterr().err
    assert await run("create-admin", "newuser", settings, monkeypatch, "short") == 1
    assert await _hash_of(engine, "newuser") is None
    assert await run("create-admin", "a", settings, monkeypatch, "a long enough pw") == 1


async def test_set_password_changes_it_and_revokes_all_sessions(
    settings: Settings,
    app: object,
    admin: Admin,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with app.state.session_factory() as session:  # type: ignore[attr-defined]
        user = await auth.authenticate(session, admin.username, admin.password)
        assert user is not None
        token = await auth.create_session(session, user, "127.0.0.1", "pytest")
    reset = await run("set-password", admin.username, settings, monkeypatch, "brand new pw!")
    assert reset == 0
    stored = await _hash_of(engine, admin.username)
    assert stored is not None
    assert passwords.verify_password_sync(stored, "brand new pw!")
    async with engine.connect() as conn:
        count: int = (
            await conn.execute(
                text("SELECT count(*) FROM sessions WHERE token_hash = :h"),
                {"h": hash_token(token)},
            )
        ).scalar_one()
    assert count == 0
    assert await run("set-password", "ghost", settings, monkeypatch, "brand new pw!") == 1


async def test_purge_sessions_command(
    settings: Settings, admin: Admin, engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await asyncio.to_thread(cli.main, ["purge-sessions"], settings) == 0
    assert "Purged 0" in capsys.readouterr().out
