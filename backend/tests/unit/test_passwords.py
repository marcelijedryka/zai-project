import pytest

from app.security import passwords
from app.security.tokens import hash_token, new_token


def test_production_parameters_are_argon2id_rfc9106_low_memory() -> None:
    hasher = passwords.make_hasher()
    encoded = hasher.hash("pw")
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert hasher.verify(encoded, "pw")
    assert not hasher.check_needs_rehash(encoded)


def test_verify_accepts_right_and_rejects_wrong_or_garbage() -> None:
    encoded = passwords.hash_password_sync("s3cret-pw")
    assert passwords.verify_password_sync(encoded, "s3cret-pw")
    assert not passwords.verify_password_sync(encoded, "other")
    assert not passwords.verify_password_sync("not-a-hash", "s3cret-pw")
    assert not passwords.verify_password_sync("", "s3cret-pw")


def test_hashes_are_salted() -> None:
    assert passwords.hash_password_sync("same") != passwords.hash_password_sync("same")


def test_needs_rehash_detects_weaker_parameters() -> None:
    weak = passwords.make_hasher(1, 8, 1).hash("pw")
    assert passwords.needs_rehash(weak)
    assert not passwords.needs_rehash(passwords.hash_password_sync("pw"))


def test_dummy_hash_is_a_valid_argon2id_hash() -> None:
    assert passwords.dummy_hash().startswith("$argon2id$")
    assert not passwords.verify_password_sync(passwords.dummy_hash(), "guess")


@pytest.mark.asyncio
async def test_async_wrappers_round_trip() -> None:
    encoded = await passwords.hash_password("pw-12345")
    assert await passwords.verify_password(encoded, "pw-12345")
    assert not await passwords.verify_password(encoded, "nope")


def test_tokens_are_long_unique_and_hashed_as_sha256_hex() -> None:
    first, second = new_token(), new_token()
    assert first != second
    assert len(first) >= 43  # 32 random bytes, url-safe base64
    digest = hash_token(first)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert first not in digest
    assert hash_token(first) == digest
