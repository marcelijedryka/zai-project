"""Opaque session tokens (T5.3): random, stored only as SHA-256."""

import hashlib
import secrets

SESSION_TTL_SECONDS = 2 * 60 * 60
COOKIE_NAME = "__Host-zai_session"  # __Host- => Secure, Path=/, no Domain, host-only


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 is enough here: the token already has 256 bits of entropy."""
    return hashlib.sha256(token.encode()).hexdigest()
