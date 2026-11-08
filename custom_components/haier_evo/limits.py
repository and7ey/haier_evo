"""Call-rate limiter without third-party dependencies.

Replaces the unmaintained ``ratelimit`` package: at most ``calls`` calls per
``period`` seconds; the period can be stretched after 429/5xx answers
(``add_period``, capped by ``max``) and reset from another thread, which also
wakes up a caller sleeping in ``sleep_and_retry``.
"""
import threading
import time
from functools import wraps


class RateLimitException(Exception):

    def __init__(self, message: str, period_remaining: float) -> None:
        super().__init__(message)
        self.period_remaining = period_remaining


class ResettableLimits:

    def __init__(self, calls: int = 1, period: float = 1, max: float | None = None) -> None:  # noqa: A002
        self.calls = int(calls) if calls >= 1 else 1
        self.period = period
        self._init_period = period
        self._max = period if max is None else max
        self.num_calls = 0
        self.last_reset = time.monotonic()
        self._lock = threading.RLock()
        self._wakeup = threading.Event()

    def _period_remaining(self) -> float:
        return self.period - (time.monotonic() - self.last_reset)

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with self._lock:
                remaining = self._period_remaining()
                if remaining <= 0:
                    self.num_calls = 0
                    self.last_reset = time.monotonic()
                self.num_calls += 1
                if self.num_calls > self.calls:
                    raise RateLimitException("too many calls", remaining)
            return func(*args, **kwargs)
        return wrapper

    def sleep_and_retry(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            while True:
                try:
                    return func(*args, **kwargs)
                except RateLimitException as exception:
                    self._sleep(exception.period_remaining)
        return wrapper

    def _sleep(self, period_remaining: float) -> None:
        self._wakeup.clear()
        self._wakeup.wait(max(0.0, period_remaining))

    def set_period(self, period: float | None = None) -> None:
        self.period = period or self._init_period

    def add_period(self, period: float) -> float:
        self.period = min(self.period + period, self._max)
        return self.period

    def copy(self) -> "ResettableLimits":
        """A fresh limiter with the same settings (its own counters and waits)."""
        return ResettableLimits(calls=self.calls, period=self._init_period, max=self._max)

    def reset(self) -> None:
        with self._lock:
            self.period = self._init_period
            self.num_calls = 0
            self.last_reset = time.monotonic()
        self._wakeup.set()
