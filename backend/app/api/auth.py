"""Admin authentication: login, logout, password change, current user."""

from fastapi import APIRouter, Request, Response

from app.core.problems import ProblemError
from app.db import SessionDep
from app.schemas.auth import LoginRequest, LoginResponse, Me, PasswordChange
from app.security.deps import BEARER_CHALLENGE, AdminDep, ThrottleDep
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


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    throttle: ThrottleDep,
) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    key = f"login:{ip}:{body.username.lower()}"
    if (wait := throttle.retry_after(key)) is not None:
        raise _too_many(wait)
    user = await auth.authenticate(session, body.username, body.password)
    if user is None:
        throttle.record_failure(key)
        raise ProblemError(401, "Invalid username or password.", headers=BEARER_CHALLENGE)
    throttle.reset(key)
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
    if (wait := throttle.retry_after(key)) is not None:
        raise _too_many(wait)
    try:
        await auth.change_password(session, admin, body.current_password, body.new_password)
    except auth.WrongCurrentPassword:
        throttle.record_failure(key)
        raise ProblemError(403, "The current password is incorrect.") from None
    except auth.PasswordRuleViolation as exc:
        raise ProblemError(422, str(exc), type_="urn:zai:problem:password-policy") from None
    throttle.reset(key)
    return Response(status_code=204)


@router.get("/me")
async def me(admin: AdminDep) -> Me:
    return Me(id=admin.user_id, username=admin.username)
