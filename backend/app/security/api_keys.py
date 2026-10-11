"""Sensor API keys (T5.5): random, shown once, stored only as SHA-256."""

import hashlib
import secrets

KEY_PREFIX = "zai_"
MAX_KEY_LENGTH = 128  # real keys are 47 characters; anything longer is rejected before hashing


def generate_api_key() -> str:
    """``zai_`` + 256 random bits (url-safe base64): 47 characters."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """SHA-256 suffices: the key already carries 256 bits of entropy (unlike a password)."""
    return hashlib.sha256(key.encode()).hexdigest()
