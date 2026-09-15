from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class ProviderRequestRateLimiter:
    """Thread-safe paced sliding-window limiter shared by one provider instance.

    ``requests_per_minute`` is both a hard 60-second window ceiling and a pacing
    target. Pacing prevents a fresh campaign from spending the whole minute's
    allowance in one burst. The limit can be lowered in place so all active game
    workers immediately share the same reduced ceiling.
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
        self._next_allowed_at = 0.0

    @property
    def minimum_interval_seconds(self) -> float:
        with self._lock:
            return self.window_s / float(self.requests_per_minute)

    def set_requests_per_minute(self, requests_per_minute: int) -> int:
        """Change the shared ceiling without replacing the limiter object."""
        limit = int(requests_per_minute)
        if limit <= 0:
            raise ValueError("requests_per_minute must be positive")
        with self._lock:
            self.requests_per_minute = limit
            if self._timestamps:
                new_interval = self.window_s / float(limit)
                self._next_allowed_at = max(
                    self._next_allowed_at,
                    self._timestamps[-1] + new_interval,
                )
            return self.requests_per_minute

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def acquire(self, *, stop_event: threading.Event | None = None) -> bool:
        """Reserve one request slot, waiting for both pacing and window capacity."""
        while True:
            if stop_event is not None and stop_event.is_set():
                return False

            with self._lock:
                now = self._clock()
                self._prune_locked(now)
                interval = self.window_s / float(self.requests_per_minute)

                window_wait = 0.0
                if len(self._timestamps) >= self.requests_per_minute:
                    window_wait = max(0.0, self._timestamps[0] + self.window_s - now)

                pace_wait = 0.0
                if self._total_acquired > 0:
                    pace_wait = max(0.0, self._next_allowed_at - now)

                wait_s = max(window_wait, pace_wait)
                if wait_s <= 0:
                    self._timestamps.append(now)
                    self._total_acquired += 1
                    self._next_allowed_at = now + interval
                    return True

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
                "minimum_interval_seconds": self.window_s / float(self.requests_per_minute),
                "requests_in_current_window": len(self._timestamps),
                "total_acquired": self._total_acquired,
            }
