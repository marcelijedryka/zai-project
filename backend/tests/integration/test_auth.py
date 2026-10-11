"""Login, logout, password change, /me, the session cookie and ``require_admin`` (plan §5, §6)."""

import re
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.security import passwords
from app.security.rate_limit import LoginThrottle, ip_throttle_key
from app.security.tokens import COOKIE_NAME, hash_token
from app.services import auth, users
from tests.conftest import ADMIN_PASSWORD, Admin

ALLOWED_ORIGIN = "http://localhost:5173"
FORGED_JWT = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhZG1pbiJ9."
FORBIDDEN_KEYS = re.compile(
    r'"(password|passwordHash|password_hash|hash|salt|apiKeyHash|api_key_hash|keyHash)"\s*:', re.I
)


async def login(client: httpx.AsyncClient, admin: Admin, password: str | None = None) -> str:
    response = await client.post(
        "/api/auth/login",
        json={"username": admin.username, "password": password or admin.password},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["accessToken"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={token}"}


async def session_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM sessions"))).scalar_one())


# --- login -------------------------------------------------------------------------


async def test_login_returns_a_bearer_token(client: httpx.AsyncClient, admin: Admin) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"accessToken", "tokenType", "expiresIn"}
    assert body["tokenType"] == "Bearer"
    assert body["expiresIn"] == 7200
    assert len(body["accessToken"]) >= 43
    assert response.headers["cache-control"] == "no-store"
    assert not FORBIDDEN_KEYS.search(response.text)
    assert admin.password not in response.text


async def test_login_sets_a_hardened_session_cookie(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    [set_cookie] = response.headers.get_list("set-cookie")
    low = set_cookie.lower()
    assert set_cookie.startswith(f"{COOKIE_NAME}=")
    for flag in ("httponly", "secure", "samesite=strict", "path=/", "max-age=7200"):
        assert flag in low, flag
    assert "domain" not in low  # required by the __Host- prefix
    assert set_cookie.split(";")[0].split("=", 1)[1] == response.json()["accessToken"]


async def test_only_the_token_hash_is_stored(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    token = await login(client, admin)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM sessions"))).mappings().all()
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hash_token(token)
    assert token not in {str(v) for v in rows[0].values()}
    ttl = rows[0]["expires_at"] - rows[0]["created_at"]
    assert timedelta(seconds=7195) < ttl < timedelta(seconds=7205)


@pytest.mark.parametrize("username", ["admin", "nobody"])
async def test_wrong_credentials_are_401_with_an_identical_body(
    client: httpx.AsyncClient, admin: Admin, username: str
) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "Invalid username or password."  # no user enumeration


async def test_unknown_user_still_pays_for_a_password_verification(
    client: httpx.AsyncClient, admin: Admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = passwords.verify_password

    async def spy(password_hash: str, password: str) -> bool:
        seen.append(password_hash)
        return await real(password_hash, password)

    monkeypatch.setattr(auth, "verify_password", spy)
    await client.post("/api/auth/login", json={"username": "nobody", "password": "x" * 12})
    assert seen == [passwords.dummy_hash()]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"username": "admin"},
        {"password": "x"},
        {"username": 5, "password": "x"},
        {"username": "admin", "password": ["x"]},
        {"username": "", "password": "x"},
        {"username": "admin", "password": "x" * 1025},
        [],
    ],
)
async def test_invalid_login_bodies_are_400(
    client: httpx.AsyncClient, admin: Admin, body: Any
) -> None:
    response = await client.post("/api/auth/login", json=body)
    assert response.status_code == 400
    assert response.json()["errors"]


async def test_malformed_login_json_is_400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", content='{"username":', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


async def test_login_rehashes_passwords_stored_with_weaker_parameters(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    weak = passwords.make_hasher(1, 8, 1).hash(admin.password)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET password_hash = :h"), {"h": weak})
    await login(client, admin)
    async with engine.connect() as conn:
        stored: str = (await conn.execute(text("SELECT password_hash FROM users"))).scalar_one()
    assert stored != weak
    assert not passwords.needs_rehash(stored)
    assert passwords.verify_password_sync(stored, admin.password)


# --- throttle ----------------------------------------------------------------------


async def test_one_failure_does_not_throttle(client: httpx.AsyncClient, admin: Admin) -> None:
    bad = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": "wrong-password"}
    )
    assert bad.status_code == 401
    await login(client, admin)


async def test_ten_failures_lock_the_key_even_for_the_right_password(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    for _ in range(10):
        r = await client.post(
            "/api/auth/login", json={"username": admin.username, "password": "wrong-password"}
        )
        assert r.status_code == 401
    locked = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert locked.headers["content-type"].startswith("application/problem+json")
    other = await client.post(
        "/api/auth/login", json={"username": "someone-else", "password": "wrong-password"}
    )
    assert other.status_code == 401  # a different (ip, username) key is unaffected


async def test_throttle_expires_and_successes_reset_it(
    app: FastAPI, client: httpx.AsyncClient, admin: Admin
) -> None:
    now = [0.0]
    app.state.login_throttle = LoginThrottle(clock=lambda: now[0])
    wrong = {"username": admin.username, "password": "wrong-password"}
    for _ in range(9):
        await client.post("/api/auth/login", json=wrong)
    await login(client, admin)  # success resets the counter
    for _ in range(9):
        await client.post("/api/auth/login", json=wrong)
    assert (await client.post("/api/auth/login", json=wrong)).status_code == 401  # still open
    assert (await client.post("/api/auth/login", json=wrong)).status_code == 429
    now[0] += 15 * 60 + 1
    await login(client, admin)


# --- require_admin -----------------------------------------------------------------


async def test_bearer_and_cookie_both_authenticate(client: httpx.AsyncClient, admin: Admin) -> None:
    token = await login(client, admin)
    for headers in (bearer(token), cookie(token), {"Authorization": f"bearer {token}"}):
        response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 200, headers
        assert response.json() == {"id": admin.id, "username": admin.username}
        assert not FORBIDDEN_KEYS.search(response.text)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {FORGED_JWT}"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic YWRtaW46cGFzcw=="},
        {"Authorization": "totally-wrong"},
        {"Cookie": f"{COOKIE_NAME}=garbage"},
    ],
)
async def test_missing_or_invalid_credentials_are_401(
    client: httpx.AsyncClient, admin: Admin, headers: dict[str, str]
) -> None:
    response = await client.get("/api/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_an_invalid_authorization_header_is_not_rescued_by_a_valid_cookie(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    token = await login(client, admin)
    response = await client.get(
        "/api/auth/me", headers={**cookie(token), "Authorization": f"Bearer {FORGED_JWT}"}
    )
    assert response.status_code == 401


async def test_expired_sessions_are_rejected(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    token = await login(client, admin)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 second'"))
    assert (await client.get("/api/auth/me", headers=bearer(token))).status_code == 401


async def test_last_seen_is_refreshed_only_when_stale(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    token = await login(client, admin)

    async def last_seen() -> Any:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT last_seen_at FROM sessions"))).scalar_one()

    first = await last_seen()
    await client.get("/api/auth/me", headers=bearer(token))
    assert await last_seen() == first  # fresh: no write
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET last_seen_at = now() - interval '10 minutes'"))
    stale = await last_seen()
    await client.get("/api/auth/me", headers=bearer(token))
    assert await last_seen() > stale


async def test_cookie_authenticated_unsafe_requests_need_an_allowed_origin(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    token = await login(client, admin)
    no_origin = await client.post("/api/auth/logout", headers=cookie(token))
    evil = await client.post(
        "/api/auth/logout", headers={**cookie(token), "Origin": "https://evil.example"}
    )
    assert (no_origin.status_code, evil.status_code) == (403, 403)
    assert no_origin.headers["content-type"].startswith("application/problem+json")
    # Safe methods and Bearer auth are not subject to the Origin check.
    assert (await client.get("/api/auth/me", headers=cookie(token))).status_code == 200
    ok = await client.post("/api/auth/logout", headers={**cookie(token), "Origin": ALLOWED_ORIGIN})
    assert ok.status_code == 204


async def test_every_mutating_admin_route_requires_authentication(app: FastAPI) -> None:
    """Auth runs before body validation and before 404 for every unsafe /api route (T5.4).

    Routes are enumerated from the OpenAPI schema (``app.routes`` hides included routers),
    so routers added in later steps are covered automatically.
    """
    public = {("post", "/api/auth/login"), ("post", "/api/measurements")}
    unsafe = {"post", "put", "patch", "delete"}
    checked: list[str] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path, operations in app.openapi()["paths"].items():
            for method in unsafe & operations.keys():
                if (method, path) in public:
                    continue
                url = re.sub(r"\{[^}]+\}", "999999", path)
                response = await client.request(method.upper(), url, json={})
                assert response.status_code == 401, f"{method.upper()} {path}"
                checked.append(f"{method.upper()} {path}")
    # Logout and password change today; later routers are covered automatically.
    assert {"POST /api/auth/logout", "PUT /api/auth/password"} <= set(checked)


# --- logout ------------------------------------------------------------------------


async def test_logout_deletes_the_session_and_expires_the_cookie(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    token = await login(client, admin)
    response = await client.post("/api/auth/logout", headers=bearer(token))  # no body, no 415
    assert response.status_code == 204
    assert response.content == b""
    [set_cookie] = response.headers.get_list("set-cookie")
    assert set_cookie.startswith(f'{COOKIE_NAME}=""') or "max-age=0" in set_cookie.lower()
    assert "path=/" in set_cookie.lower()
    assert await session_count(engine) == 0
    assert (await client.get("/api/auth/me", headers=bearer(token))).status_code == 401
    assert (await client.post("/api/auth/logout", headers=bearer(token))).status_code == 401


async def test_logout_only_ends_the_current_session(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    first, second = await login(client, admin), await login(client, admin)
    await client.post("/api/auth/logout", headers=bearer(first))
    assert (await client.get("/api/auth/me", headers=bearer(second))).status_code == 200
    assert await session_count(engine) == 1


# --- password change -----------------------------------------------------------------


NEW_PASSWORD = "a brand new passphrase"


def password_body(current: str = ADMIN_PASSWORD, new: str = NEW_PASSWORD) -> dict[str, str]:
    return {"currentPassword": current, "newPassword": new}


async def test_password_change_succeeds_and_revokes_other_sessions(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    current, other = await login(client, admin), await login(client, admin)
    response = await client.put("/api/auth/password", json=password_body(), headers=bearer(current))
    assert response.status_code == 204
    assert (await client.get("/api/auth/me", headers=bearer(current))).status_code == 200
    assert (await client.get("/api/auth/me", headers=bearer(other))).status_code == 401
    assert await session_count(engine) == 1
    old_login = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    assert old_login.status_code == 401
    await login(client, admin, password=NEW_PASSWORD)


async def test_password_change_updates_the_timestamp_and_stores_an_argon2id_hash(
    client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    token = await login(client, admin)
    await client.put("/api/auth/password", json=password_body(), headers=bearer(token))
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT password_hash, password_changed_at > created_at AS changed FROM users")
            )
        ).one()
    assert row.password_hash.startswith("$argon2id$")
    assert "brand new" not in row.password_hash
    assert row.changed


async def test_password_change_status_precedence(client: httpx.AsyncClient, admin: Admin) -> None:
    token = await login(client, admin)
    put = client.put
    # 401 before everything else, including body validation (but not malformed JSON).
    assert (await put("/api/auth/password", json=password_body())).status_code == 401
    assert (await put("/api/auth/password", json={"x": 1})).status_code == 401
    # 400: invalid body, even if the current password would also be wrong.
    missing = await put("/api/auth/password", json={"newPassword": "x"}, headers=bearer(token))
    too_long = await put(
        "/api/auth/password", json=password_body(new="x" * 1025), headers=bearer(token)
    )
    assert (missing.status_code, too_long.status_code) == (400, 400)
    # 403: wrong current password, even if the new one is also unacceptable.
    wrong = password_body(current="not my password", new="short")
    assert (await put("/api/auth/password", json=wrong, headers=bearer(token))).status_code == 403
    # 422: semantic rules on the new password.
    short = await put("/api/auth/password", json=password_body(new="short"), headers=bearer(token))
    same = await put(
        "/api/auth/password", json=password_body(new=ADMIN_PASSWORD), headers=bearer(token)
    )
    assert (short.status_code, same.status_code) == (422, 422)
    assert short.headers["content-type"].startswith("application/problem+json")
    assert ADMIN_PASSWORD not in same.text


async def test_wrong_current_password_attempts_are_throttled(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    token = await login(client, admin)
    wrong = password_body(current="not my password")
    for _ in range(10):
        r = await client.put("/api/auth/password", json=wrong, headers=bearer(token))
        assert r.status_code == 403
    locked = await client.put("/api/auth/password", json=password_body(), headers=bearer(token))
    assert locked.status_code == 429


# --- services ------------------------------------------------------------------------


async def test_purge_removes_only_expired_sessions(
    app: FastAPI, client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    expired = await login(client, admin)
    fresh = await login(client, admin)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET expires_at = now() - interval '1 hour' WHERE token_hash = :h"
            ),
            {"h": hash_token(expired)},
        )
    async with app.state.session_factory() as session:
        assert await auth.purge_expired_sessions(session) == 1
    assert (await client.get("/api/auth/me", headers=bearer(fresh))).status_code == 200
    assert await session_count(engine) == 1


async def test_usernames_are_unique_and_validated(app: FastAPI, admin: Admin) -> None:
    async with app.state.session_factory() as session:
        with pytest.raises(users.UsernameTaken):
            await users.create_user(session, admin.username, "another password")
        with pytest.raises(users.UserError):
            await users.create_user(session, "x", "another password")
        with pytest.raises(users.UserError):
            await users.create_user(session, "valid-name", "short")


async def test_background_task_purges_expired_sessions(
    app: FastAPI, client: httpx.AsyncClient, admin: Admin, engine: AsyncEngine
) -> None:
    import asyncio

    from app.tasks import housekeeping_forever

    await login(client, admin)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 hour'"))
    task = asyncio.create_task(housekeeping_forever(app.state.session_factory, 30, 0.01))
    try:
        for _ in range(100):
            if await session_count(engine) == 0:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
    assert await session_count(engine) == 0


# --- throttle under concurrency, per-IP limit, bounded hashing ---------------------------


async def test_a_burst_of_parallel_wrong_logins_cannot_exceed_the_failure_limit(
    client: httpx.AsyncClient, admin: Admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    reached = 0
    real = auth.authenticate

    async def spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal reached
        reached += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(auth, "authenticate", spy)
    body = {"username": admin.username, "password": "wrong-password"}
    responses = await asyncio.gather(
        *[client.post("/api/auth/login", json=body) for _ in range(30)]
    )
    codes = [r.status_code for r in responses]
    assert reached == 10  # only max_failures guesses were ever verified
    assert codes.count(401) == 10
    assert codes.count(429) == 20
    assert all("retry-after" in r.headers for r in responses if r.status_code == 429)


async def test_a_burst_of_parallel_wrong_current_passwords_is_limited_too(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    import asyncio

    token = await login(client, admin)
    wrong = password_body(current="not my password")
    responses = await asyncio.gather(
        *[client.put("/api/auth/password", json=wrong, headers=bearer(token)) for _ in range(30)]
    )
    codes = [r.status_code for r in responses]
    assert (codes.count(403), codes.count(429)) == (10, 20)


async def test_varying_the_username_still_hits_the_per_client_limit(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    codes = []
    for i in range(35):
        r = await client.post(
            "/api/auth/login", json={"username": f"guess{i}", "password": "wrong-password"}
        )
        codes.append(r.status_code)
    assert codes[:30] == [401] * 30
    assert codes[30:] == [429] * 5
    assert (
        await client.post(
            "/api/auth/login", json={"username": admin.username, "password": admin.password}
        )
    ).status_code == 429  # the whole client is locked, whatever the username


async def test_a_successful_login_does_not_consume_the_client_budget(
    app: FastAPI, client: httpx.AsyncClient, admin: Admin
) -> None:
    for i in range(5):
        await client.post("/api/auth/login", json={"username": f"guess{i}", "password": "x" * 10})
    await login(client, admin)
    key = ip_throttle_key("127.0.0.1")
    assert len(app.state.ip_throttle._failures[key]) == 5  # the success gave its slot back
    assert f"{key}:{admin.username}" not in app.state.login_throttle._failures


async def test_password_hashing_concurrency_and_queue_are_bounded(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading
    import time

    running = peak = 0
    lock = threading.Lock()

    def slow_verify(password_hash: str, password: str) -> bool:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.1)
        with lock:
            running -= 1
        return False

    monkeypatch.setattr(passwords, "verify_password_sync", slow_verify)
    monkeypatch.setattr(passwords, "HASH_MAX_PENDING", 4)
    responses = await asyncio.gather(
        *[
            client.post("/api/auth/login", json={"username": f"user{i}", "password": "x" * 10})
            for i in range(14)
        ]
    )
    codes = [r.status_code for r in responses]
    assert set(codes) == {401, 503}
    assert codes.count(503) >= 1
    assert peak <= passwords.HASH_WORKERS
    busy = next(r for r in responses if r.status_code == 503)
    assert busy.headers["content-type"].startswith("application/problem+json")
    assert busy.headers["retry-after"] == "1"
    # Refused attempts were not wrong guesses, so they do not count against the client.
    key = ip_throttle_key("127.0.0.1")
    assert len(app.state.ip_throttle._failures[key]) == codes.count(401)


async def test_the_dummy_hash_is_warmed_up_at_startup(app: FastAPI) -> None:
    assert passwords.dummy_hash.cache_info().currsize == 1
