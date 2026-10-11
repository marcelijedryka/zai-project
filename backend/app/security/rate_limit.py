"""Failed-attempt throttle (in memory, per worker).

Only failures count; a success resets the key. With several uvicorn workers each keeps its
own counters, so the effective limit is at most ``workers * max_failures`` per window.
"""

import math
import time
from collections import deque
from collections.abc import Callable


class LoginThrottle:
    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        failures = self._failures.get(key)
        if failures is None:
            return deque()
        cutoff = self._clock() - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
        return failures

    def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may try again, or ``None`` if it is not locked out."""
        failures = self._recent(key)
        if len(failures) < self.max_failures:
            return None
        return max(1, math.ceil(failures[0] + self.window_seconds - self._clock()))

    def record_failure(self, key: str) -> None:
        if len(self._failures) >= self._max_keys:
            for stale in list(self._failures):
                self._recent(stale)  # drops expired entries
            if len(self._failures) >= self._max_keys:
                self._failures.pop(next(iter(self._failures)))  # memory bound: evict the oldest
        self._failures.setdefault(key, deque()).append(self._clock())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
