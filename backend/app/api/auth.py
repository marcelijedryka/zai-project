"""Admin authentication: login, logout, password change, current user."""

from fastapi import APIRouter, Request, Response

from app.core.problems import ProblemError, problem_response
from app.db import SessionDep
from app.schemas.auth import LoginRequest, LoginResponse, Me, PasswordChange
from app.security.deps import BEARER_CHALLENGE, AdminDep, IpThrottleDep, ThrottleDep
from app.security.rate_limit import ip_throttle_key
from app.security.tokens import COOKIE_NAME, SESSION_TTL_SECONDS
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_FLAGS = {"path": "/", "secure": True, "httponly": True, "samesite": "strict"}


def _too_many(retry_after: int) -> ProblemError:
    return ProblemError(
        429,
        "Too many failed attempts. Try again later.",
        headers={"Retry-After": str(retry_after)},
    )


async def hashing_busy_handler(request: Request, exc: Exception) -> Response:
    return problem_response(
        503,
        instance=request.url.path,
        detail="The server is busy. Try again shortly.",
        headers={"Retry-After": "1"},
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    throttle: ThrottleDep,
    ip_throttle: IpThrottleDep,
) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    ip_key = ip_throttle_key(ip)
    user_key = f"{ip_key}:{body.username.lower()}"
    # Reserve both attempts *before* the slow verification (no await in between), so a burst of
    # parallel guesses cannot all pass the check. A bad guess keeps its reservation.
    if (wait := ip_throttle.acquire(ip_key)) is not None:
        raise _too_many(wait)
    if (wait := throttle.acquire(user_key)) is not None:
        ip_throttle.release(ip_key)
        raise _too_many(wait)
    try:
        user = await auth.authenticate(session, body.username, body.password)
    except BaseException:  # busy (503), cancelled or crashed: not a wrong guess
        ip_throttle.release(ip_key)
        throttle.release(user_key)
        raise
    if user is None:
        raise ProblemError(401, "Invalid username or password.", headers=BEARER_CHALLENGE)
    ip_throttle.release(ip_key)
    throttle.reset(user_key)
    token = await auth.create_session(session, user, ip, request.headers.get("user-agent"))
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS, **COOKIE_FLAGS)  # type: ignore[arg-type]
    return LoginResponse(access_token=token, expires_in=SESSION_TTL_SECONDS)


@router.post("/logout", status_code=204, response_class=Response)
async def logout(admin: AdminDep, session: SessionDep) -> Response:
    await auth.delete_session(session, admin.session_id)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, **COOKIE_FLAGS)  # type: ignore[arg-type]
    return response


@router.put("/password", status_code=204, response_class=Response)
async def change_password(
    body: PasswordChange, admin: AdminDep, session: SessionDep, throttle: ThrottleDep
) -> Response:
    # Order: 401 (AdminDep) -> 400 (body) -> 403 (wrong current) -> 422 (new password rules).
    key = f"password:{admin.user_id}"
    if (wait := throttle.acquire(key)) is not None:  # reserved before the slow check, see login
        raise _too_many(wait)
    try:
        await auth.change_password(session, admin, body.current_password, body.new_password)
    except auth.WrongCurrentPassword:
        raise ProblemError(403, "The current password is incorrect.") from None
    except auth.PasswordRuleViolation as exc:
        throttle.release(key)  # the current password was right
        raise ProblemError(422, str(exc), type_="urn:zai:problem:password-policy") from None
    except BaseException:
        throttle.release(key)
        raise
    throttle.reset(key)
    return Response(status_code=204)


@router.get("/me")
async def me(admin: AdminDep) -> Me:
    return Me(id=admin.user_id, username=admin.username)
