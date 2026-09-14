from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class ProviderRequestRateLimiter:
    """Thread-safe sliding-window limiter shared by all games of one provider.

    A successful ``acquire`` reserves exactly one outbound provider-protocol
    request. The default window is 60 seconds so a configured value maps directly
    to requests/minute. Callers may inject clock/sleep functions for deterministic
    tests.
    """

    def __init__(
        self,
        *,
        requests_per_minute: int = 2000,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        limit = int(requests_per_minute)
        if limit <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.requests_per_minute = limit
        self.window_s = 60.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._timestamps: deque[float] = deque()
        self._total_acquired = 0

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def acquire(self, *, stop_event: threading.Event | None = None) -> bool:
        """Reserve one request slot, waiting if needed.

        Returns ``False`` when cancellation is already requested or becomes set
        while waiting. A cancelled acquisition is not counted.
        """

        while True:
            if stop_event is not None and stop_event.is_set():
                return False

            with self._lock:
                now = self._clock()
                self._prune_locked(now)
                if len(self._timestamps) < self.requests_per_minute:
                    self._timestamps.append(now)
                    self._total_acquired += 1
                    return True
                wait_s = max(0.0, self._timestamps[0] + self.window_s - now)

            if stop_event is not None:
                if stop_event.wait(wait_s):
                    return False
            else:
                self._sleep(wait_s)

    @property
    def total_acquired(self) -> int:
        with self._lock:
            return self._total_acquired

    @property
    def requests_in_current_window(self) -> int:
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            return len(self._timestamps)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            return {
                "requests_per_minute_limit": self.requests_per_minute,
                "window_seconds": self.window_s,
                "requests_in_current_window": len(self._timestamps),
                "total_acquired": self._total_acquired,
            }
