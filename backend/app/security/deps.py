"""``require_admin``: Bearer token or session cookie, with an Origin check for cookies."""

from typing import Annotated

from fastapi import Depends, Request

from app.core.problems import ProblemError
from app.db import SessionDep
from app.security.rate_limit import LoginThrottle
from app.security.tokens import COOKIE_NAME
from app.services import auth, sensors

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}
API_KEY_CHALLENGE = {"WWW-Authenticate": 'ApiKey header="X-API-Key"'}


def unauthorized(detail: str = "Authentication required.") -> ProblemError:
    return ProblemError(401, detail, headers=BEARER_CHALLENGE)


def _token_from(request: Request) -> tuple[str | None, bool]:
    """Return ``(token, from_cookie)``. An Authorization header always wins over the cookie."""
    header = request.headers.get("authorization")
    if header is not None:
        scheme, _, credentials = header.partition(" ")
        credentials = credentials.strip()
        return (credentials, False) if scheme.lower() == "bearer" and credentials else (None, False)
    return request.cookies.get(COOKIE_NAME), True


def _check_origin(request: Request) -> None:
    """CSRF defence in depth: state-changing cookie requests must come from a trusted origin.

    "Trusted" is exactly ``CORS_ALLOWED_ORIGINS``: with credentialed CORS an allow-listed origin
    can already send unsafe requests, so the two lists are deliberately the same.
    """
    allowed: list[str] = request.app.state.settings.cors_allowed_origins
    if request.headers.get("origin") not in allowed:
        raise ProblemError(403, "Cross-origin request rejected.")


async def require_admin(request: Request, session: SessionDep) -> auth.AdminIdentity:
    token, from_cookie = _token_from(request)
    if not token:
        raise unauthorized()
    identity = await auth.find_session(session, token)
    if identity is None:
        raise unauthorized("The token is invalid or has expired.")
    if from_cookie and request.method not in SAFE_METHODS:
        _check_origin(request)
    return identity


async def require_sensor(request: Request, session: SessionDep) -> sensors.SensorIdentity:
    """Authenticate a sensor by ``X-API-Key`` only; an admin Bearer token is never enough.

    Revoked keys fail at once because the lookup filters on ``revoked_at IS NULL``.
    """
    identity = await sensors.authenticate_sensor(session, request.headers.get("x-api-key", ""))
    if identity is None:
        raise ProblemError(401, "A valid sensor API key is required.", headers=API_KEY_CHALLENGE)
    return identity


def get_login_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


def get_ip_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.ip_throttle
    return throttle


AdminDep = Annotated[auth.AdminIdentity, Depends(require_admin)]
SensorDep = Annotated[sensors.SensorIdentity, Depends(require_sensor)]
ThrottleDep = Annotated[LoginThrottle, Depends(get_login_throttle)]
IpThrottleDep = Annotated[LoginThrottle, Depends(get_ip_throttle)]
