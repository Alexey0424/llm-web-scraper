"""Per-domain polite rate limiting (synchronous)."""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse


class DomainRateLimiter:
    """Enforce a minimum delay between requests to the same domain.

    Thread-safe and process-local. ``sleep_fn`` is injectable so tests can run
    without real waits.
    """

    def __init__(self, default_delay: float = 2.0, overrides: dict | None = None, sleep_fn=time.sleep, clock=time.monotonic):
        self.default_delay = default_delay
        self.overrides = overrides or {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sleep = sleep_fn
        self._clock = clock

    def _delay_for(self, domain: str) -> float:
        ov = self.overrides.get(domain)
        if isinstance(ov, dict) and "min_delay_seconds" in ov:
            return float(ov["min_delay_seconds"])
        return self.default_delay

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
