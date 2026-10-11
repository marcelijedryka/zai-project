import pytest

from app.security.rate_limit import LoginThrottle, ip_throttle_key


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(max_failures: int = 3, window: float = 60, **kw: int) -> tuple[LoginThrottle, Clock]:
    clock = Clock()
    return LoginThrottle(max_failures, window, clock=clock, **kw), clock


def test_a_single_failure_never_locks() -> None:
    throttle, _ = make()
    throttle.record_failure("k")
    assert throttle.retry_after("k") is None


def test_locks_after_max_failures_and_reports_wait() -> None:
    throttle, clock = make()
    for _ in range(3):
        throttle.record_failure("k")
        clock.now += 1
    assert throttle.retry_after("k") == 57  # t=1003; the oldest failure (t=1000) expires at 1060


def test_unlocks_when_the_window_passes() -> None:
    throttle, clock = make()
    for _ in range(3):
        throttle.record_failure("k")
    clock.now += 61
    assert throttle.retry_after("k") is None


def test_keys_are_independent() -> None:
    throttle, _ = make()
    for _ in range(3):
        throttle.record_failure("a")
    assert throttle.retry_after("a") is not None
    assert throttle.retry_after("b") is None


def test_success_reset_clears_the_counter() -> None:
    throttle, _ = make()
    for _ in range(2):
        throttle.record_failure("k")
    throttle.reset("k")
    throttle.record_failure("k")
    assert throttle.retry_after("k") is None


def test_memory_is_bounded() -> None:
    throttle, _ = make(max_keys=5)
    for i in range(50):
        throttle.record_failure(f"k{i}")
    assert len(throttle._failures) <= 5


def test_acquire_reserves_atomically_and_locks_at_the_limit() -> None:
    throttle, _ = make(max_failures=3)
    assert [throttle.acquire("k") for _ in range(3)] == [None, None, None]
    assert throttle.acquire("k") is not None  # the 4th attempt is refused...
    assert len(throttle._failures["k"]) == 3  # ...and is not itself recorded


def test_release_gives_back_one_reservation() -> None:
    throttle, _ = make(max_failures=2)
    throttle.acquire("k")
    throttle.acquire("k")
    throttle.release("k")
    assert throttle.acquire("k") is None
    throttle.release("k")
    throttle.release("k")
    throttle.release("k")  # releasing an empty key is harmless
    assert "k" not in throttle._failures


def test_eviction_drops_the_least_recently_active_key() -> None:
    throttle, clock = make(max_failures=5, max_keys=3)
    for key in ("a", "b", "c"):
        throttle.record_failure(key)
        clock.now += 1
    throttle.record_failure("a")  # "a" is active again, so "b" is now the oldest
    throttle.record_failure("d")
    assert set(throttle._failures) == {"a", "c", "d"}


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("unknown", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_ip_throttle_key_normalises_addresses(ip: str | None, expected: str) -> None:
    assert ip_throttle_key(ip) == expected


def test_ipv6_clients_in_different_64s_get_different_keys() -> None:
    assert ip_throttle_key("2001:db8:1:2::1") != ip_throttle_key("2001:db8:1:3::1")
