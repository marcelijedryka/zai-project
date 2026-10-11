"""Argon2id password hashing (T5.1). Hashing is CPU/memory bound, so it runs in a thread."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024  # bounds the work an attacker can force per request

# Each hash needs 64 MiB, so concurrency is bounded explicitly instead of using the default
# thread pool (~32 workers = ~2 GiB). Excess work queues up to HASH_MAX_PENDING, then is refused.
HASH_WORKERS = 2
HASH_MAX_PENDING = 16
_executor = ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="argon2")
_pending = 0


class HashingBusy(Exception):
    """Too many password hashes are queued; the API answers 503."""


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


async def _run[T](function: Callable[..., T], *args: object) -> T:
    global _pending
    if _pending >= HASH_MAX_PENDING:
        raise HashingBusy
    _pending += 1
    try:
        return await asyncio.get_running_loop().run_in_executor(_executor, function, *args)
    finally:
        _pending -= 1


async def hash_password(password: str) -> str:
    return await _run(hash_password_sync, password)


async def verify_password(password_hash: str, password: str) -> bool:
    return await _run(verify_password_sync, password_hash, password)


@lru_cache
def dummy_hash() -> str:
    """Hash verified for unknown usernames, so they cost as much time as real ones."""
    return hash_password_sync("zai-timing-equalisation-dummy")


async def warm_up() -> None:
    """Compute the dummy hash at startup so the first unknown-user login is not slower."""
    await _run(dummy_hash)
