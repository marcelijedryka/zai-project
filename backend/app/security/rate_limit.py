"""Failed-attempt throttle (in memory, per worker).

An attempt is *reserved* with :meth:`acquire` before the slow password check and given back
(:meth:`release`/:meth:`reset`) only if it turns out to be legitimate. Check and reservation
happen in one synchronous call, so a burst of parallel requests cannot all slip past the limit
while the first verifications are still running.

With several uvicorn workers each keeps its own counters, so the effective limit is at most
``workers * max_failures`` per window.
"""

import ipaddress
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable


def password_change_key(user_id: int) -> str:
    """Throttle key for wrong current-password attempts on ``PUT /api/auth/password``."""
    return f"password:{user_id}"


def ip_throttle_key(ip: str | None) -> str:
    """Normalise a client address for throttling: IPv6 clients share their whole /64."""
    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


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
        # Ordered by recent activity (least recently active first) for LRU eviction.
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

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
        if key not in self._failures and len(self._failures) >= self._max_keys:
            for stale in list(self._failures):
                self._recent(stale)  # drops expired entries
            if len(self._failures) >= self._max_keys:
                self._failures.popitem(last=False)  # evict the least recently active key
        self._failures.setdefault(key, deque()).append(self._clock())
        self._failures.move_to_end(key)

    def acquire(self, key: str) -> int | None:
        """Atomically check ``key`` and reserve one attempt.

        Returns ``None`` when the attempt may proceed (it now counts as a failure until released),
        or the seconds to wait when the key is locked out. Never ``await`` between this call and
        the work it guards being scheduled.
        """
        wait = self.retry_after(key)
        if wait is None:
            self.record_failure(key)
        return wait

    def release(self, key: str) -> None:
        """Give back one reservation (the attempt turned out legitimate, or never ran)."""
        failures = self._failures.get(key)
        if failures:
            failures.pop()
            if not failures:
                del self._failures[key]

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
