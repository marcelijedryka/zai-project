from app.security.rate_limit import LoginThrottle


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
