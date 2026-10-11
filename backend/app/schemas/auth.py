from typing import Literal

from pydantic import Field

from app.schemas.common import CamelModel, RequestModel
from app.security.passwords import MAX_PASSWORD_LENGTH


class LoginRequest(RequestModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class LoginResponse(CamelModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - token scheme, not a secret
    expires_in: int


class PasswordChange(RequestModel):
    # Length and "must differ" rules are semantic (422) and live in the service, not here (400).
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class Me(CamelModel):
    id: int
    username: str
