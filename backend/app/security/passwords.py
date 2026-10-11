"""Argon2id password hashing (T5.1). Hashing is CPU/memory bound, so it runs in a thread."""

import asyncio
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024  # bounds the work an attacker can force per request


def make_hasher(
    time_cost: int = 3, memory_cost: int = 64 * 1024, parallelism: int = 4
) -> PasswordHasher:
    """Defaults are the RFC 9106 "low memory" profile: Argon2id, t=3, m=64 MiB, p=4."""
    return PasswordHasher(time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism)


_hasher = make_hasher()


def hash_password_sync(password: str) -> str:
    return _hasher.hash(password)


def verify_password_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash was made with weaker parameters than the current ones."""
    return _hasher.check_needs_rehash(password_hash)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(hash_password_sync, password)


async def verify_password(password_hash: str, password: str) -> bool:
    return await asyncio.to_thread(verify_password_sync, password_hash, password)


@lru_cache
def dummy_hash() -> str:
    """Hash verified for unknown usernames, so they cost as much time as real ones."""
    return hash_password_sync("zai-timing-equalisation-dummy")
