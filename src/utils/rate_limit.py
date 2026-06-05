"""Per-domain polite rate limiting (synchronous)."""

from __future__ import annotations

import random
import threading
import time
from urllib.parse import urlparse


class DomainRateLimiter:
    """Enforce a randomized polite delay between requests to the same domain.

    The wait is a uniform random value in ``[min_delay, max_delay]`` so the
    request cadence is human-like rather than a constant, robotic gap. The
    minimum is always respected (jitter only adds time, never removes it), so
    politeness is preserved. When no maximum is configured the delay is the
    fixed minimum (original behaviour).

    Thread-safe and process-local. ``sleep_fn`` and ``rand_fn`` are injectable
    so tests can run without real waits or randomness.
    """

    def __init__(self, default_delay: float = 2.0, overrides: dict | None = None,
                 sleep_fn=time.sleep, clock=time.monotonic,
                 max_delay: float | None = None, rand_fn=random.uniform):
        self.default_delay = default_delay
        self.default_max = max_delay
        self.overrides = overrides or {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sleep = sleep_fn
        self._clock = clock
        self._rand = rand_fn

    def _delay_for(self, domain: str) -> float:
        ov = self.overrides.get(domain)
        if isinstance(ov, dict) and "min_delay_seconds" in ov:
            lo = float(ov["min_delay_seconds"])
            hi = ov.get("max_delay_seconds", self.default_max)
        else:
            lo = self.default_delay
            hi = self.default_max
        if hi is not None and float(hi) > lo:
            return self._rand(lo, float(hi))
        return lo

    def wait(self, url: str) -> float:
        """Block until it is polite to request ``url``. Returns seconds waited."""
        domain = urlparse(url).netloc.lower()
        delay = self._delay_for(domain)
        with self._lock:
            now = self._clock()
            last = self._last.get(domain)
            waited = 0.0
            if last is not None:
                elapsed = now - last
                if elapsed < delay:
                    waited = delay - elapsed
                    self._sleep(waited)
            self._last[domain] = self._clock()
            return waited
