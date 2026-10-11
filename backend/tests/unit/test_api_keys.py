import re

from app.security.api_keys import KEY_PREFIX, generate_api_key, hash_api_key


def test_keys_are_prefixed_long_random_and_urlsafe() -> None:
    keys = {generate_api_key() for _ in range(200)}
    assert len(keys) == 200
    for key in keys:
        assert key.startswith(KEY_PREFIX)
        assert len(key) == 47  # "zai_" + 43 url-safe base64 characters (256 bits)
        assert len(key) >= 32  # contract and T5.5 minimum
        assert re.fullmatch(r"zai_[A-Za-z0-9_-]{43}", key)


def test_hash_is_deterministic_sha256_hex_and_hides_the_key() -> None:
    key = generate_api_key()
    digest = hash_api_key(key)
    assert digest == hash_api_key(key)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert key not in digest
    assert digest != hash_api_key(generate_api_key())
