
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

from ..config import load_rate_limits
from ..utils import compliance
from ..utils.rate_limit import DomainRateLimiter
from ..utils.retry import with_retry

# Scrapling-only imports.
# No httpx fallback exists in this file.
try:
    from scrapling.fetchers import Fetcher, DynamicFetcher, StealthyFetcher
except ImportError:
    Fetcher = None
    DynamicFetcher = None
    StealthyFetcher = None


log = logging.getLogger(__name__)


ScraplingMode = Literal["fast", "dynamic", "stealth"]


@dataclass
class FetchResult:
    url: str
    content: str
    ok: bool
    reason: str = ""


class FetchHTTPError(RuntimeError):
    """An HTTP error status from the target site, carrying the status code."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP {status}")


# HTTP statuses that re-requesting can never fix, so a retry is pointless.
# 403/429 are deliberately NOT here: anti-bot front-ends (PerimeterX, etc.)
# 403/429 the first hit of a fresh navigation as a CHALLENGE and then serve the
# page on a warm retry — retrying those is what makes protected sites work
# (proven by run #44; removing it broke #57). A crawl that keeps failing is
# bounded by the discovery loop's consecutive-failure circuit breaker instead.
_PERMANENT_STATUS = {400, 401, 404, 405, 410}


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, FetchHTTPError):
        return exc.status not in _PERMANENT_STATUS
    return True  # network/transport errors are transient → retry


BLOCKED_PAGE_MARKERS = (
    "access to this page has been denied",
    "verify you are a human",
    "are you a human",
    "captcha",
    "unusual traffic",
    "request blocked",
    "access denied",
)


# Markup that must be removed before scanning for block markers: virtually
# every form page embeds reCAPTCHA *assets* (script src, g-recaptcha divs,
# "protected by reCAPTCHA" badges) without being a challenge page.
_BLOCK_STRIP_RE = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>")
_HTML_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_ANY_TAG_RE = re.compile(r"(?s)<[^>]+>")
_BENIGN_PHRASES = ("protected by recaptcha",)


def detect_blocked_page(html: str) -> Optional[str]:
    """
    Return a specific reason if the fetched HTML looks like an inaccessible,
    blocked, CAPTCHA, or anti-automation page.

    Only the page's *visible text* is scanned (scripts/styles/tags stripped) so
    that reCAPTCHA assets on a normal page don't get the page silently dropped
    as "blocked". Challenge pages state the block in their visible text, and
    they lead with it, so only the head of the text is checked.

    This does not try to bypass the block. It only reports it clearly.
    """
    if not html:
        return "empty response"

    text = _BLOCK_STRIP_RE.sub(" ", html[:200_000])
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _ANY_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).lower()
    for phrase in _BENIGN_PHRASES:
        text = text.replace(phrase, " ")
    head = text[:4000]

    for marker in BLOCKED_PAGE_MARKERS:
        if marker in head:
            return f"blocked page detected: {marker!r}"

    return None


def scrapling_response_to_html(page: Any) -> str:
    """
    Robustly extract HTML/text from a Scrapling response object.

    Scrapling response objects may expose content differently depending on
    fetcher type and version, so we check several common attributes.
    """
    for attr in ("html", "text", "content"):
        value = getattr(page, attr, None)

        if isinstance(value, str) and value.strip():
            return value

        if isinstance(value, bytes) and value.strip():
            return value.decode("utf-8", errors="replace")

    body = getattr(page, "body", None)

    if isinstance(body, str) and body.strip():
        return body

    if isinstance(body, bytes) and body.strip():
        return body.decode("utf-8", errors="replace")

    return str(page)


def scrapling_response_status(page: Any) -> int:
    """
    Extract status code from a Scrapling response object.

    If no status is exposed, assume 200 because Scrapling successfully
    returned a page-like object. This avoids crashing on response objects
    that do not expose status/status_code.
    """
    for attr in ("status", "status_code"):
        value = getattr(page, attr, None)

        if isinstance(value, int):
            return value

        if isinstance(value, str) and value.isdigit():
            return int(value)

    log.debug("Scrapling response had no status/status_code; assuming 200")
    return 200


class BaseFetcher:
    """
    Shared fetcher logic.

    This class handles:
    - the URL gate (Direct Mode: scheme check only; robots.txt currently off)
    - per-domain rate limiting
    - retries
    - blocked-page detection
    - standard FetchResult formatting

    It does not implement any network transport itself.
    """

    engine = "base"

    def __init__(self, limiter: Optional[DomainRateLimiter] = None):
        rate_limits = load_rate_limits()
        rl = rate_limits.get("defaults", {})

        self.user_agent = rl.get(
            "user_agent",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self.timeout = rl.get("request_timeout_seconds", 30)
        self.max_retries = rl.get("max_retries", 3)
        self.backoff = rl.get("backoff_base_seconds", 1.5)

        self.limiter = limiter or DomainRateLimiter(
            default_delay=rl.get("min_delay_seconds", 2.0),
            max_delay=rl.get("max_delay_seconds"),
            overrides=rate_limits.get("overrides", {}),
        )

    def close(self) -> None:
        """
        Kept for compatibility with code that expects fetchers to have close().
        Scrapling class-level fetch calls do not require an owned httpx client.
        """
        pass

    def _raw_get(self, url: str) -> tuple[int, str]:
        """
        Subclasses must implement the actual network call.
        """
        raise NotImplementedError("Subclasses must implement _raw_get().")

    def get(self, url: str) -> FetchResult:
        """
        Main entry point for all fetches.
        """
        gate = compliance.gate_url(url, self.user_agent)

        if not gate.allowed:
            return FetchResult(
                url=url,
                content="",
                ok=False,
                reason=gate.reason,
            )

        self.limiter.wait(url)

        @with_retry(max_retries=self.max_retries, base=self.backoff,
                    should_retry=_retryable)
        def _do() -> tuple[int, str]:
            return self._raw_get(url)

        try:
            status, text = _do()

            blocked_reason = detect_blocked_page(text)
            if blocked_reason:
                return FetchResult(
                    url=url,
                    content="",
                    ok=False,
                    reason=blocked_reason,
                )

            return FetchResult(
                url=url,
                content=text,
                ok=True,
            )

        except Exception as exc:
            log.warning("fetch failed %s: %s", url, exc)

            return FetchResult(
                url=url,
                content="",
                ok=False,
                reason=str(exc),
            )


class ScraplingFetcher(BaseFetcher):
    """
    Scrapling-only fetcher.

    No httpx fallback.
    No alternate fallback engine.

    Modes:
    - fast:
        Uses Scrapling Fetcher.get().
        Static HTTP with browser-like TLS/headers.
        No JavaScript rendering.

    - dynamic:
        Uses Scrapling DynamicFetcher.fetch().
        Browser-based rendering for JavaScript-heavy pages.

    - stealth:
        Uses Scrapling StealthyFetcher.fetch().
        Browser-based rendering with stealth features.
    """

    engine = "scrapling"

    def __init__(
        self,
        mode: str = "stealth",
        limiter: Optional[DomainRateLimiter] = None,
        wait_selector: Optional[str] = None,
    ):
        super().__init__(limiter=limiter)

        normalized_mode = mode.strip().lower()

        if normalized_mode not in {"fast", "dynamic", "stealth"}:
            raise ValueError(f"Unknown Scrapling mode: {mode!r}")

        self.mode: ScraplingMode = normalized_mode  # type: ignore[assignment]
        self.wait_selector = wait_selector

        self._validate_scrapling_available()

    def _validate_scrapling_available(self) -> None:
        """
        Fail immediately if the selected Scrapling fetcher is unavailable.
        This is intentional: there are no fallbacks.
        """
        if self.mode == "fast" and Fetcher is None:
            raise RuntimeError(
                "Scrapling mode 'fast' requested, but scrapling.fetchers.Fetcher "
                "is not available. Install Scrapling with: pip install scrapling"
            )

        if self.mode == "dynamic" and DynamicFetcher is None:
            raise RuntimeError(
                "Scrapling mode 'dynamic' requested, but "
                "scrapling.fetchers.DynamicFetcher is not available. "
                "Install Scrapling with: pip install scrapling"
            )

        if self.mode == "stealth" and StealthyFetcher is None:
            raise RuntimeError(
                "Scrapling mode 'stealth' requested, but "
                "scrapling.fetchers.StealthyFetcher is not available. "
                "Install Scrapling with: pip install scrapling"
            )

    def _raw_get(self, url: str) -> tuple[int, str]:
        """
        Perform a Scrapling fetch.

        This method intentionally does not fallback to httpx or any other engine.
        """
        if self.mode == "fast":
            page = Fetcher.get(
                url,
                impersonate="chrome",
                stealthy_headers=True,
                timeout=self.timeout,  # seconds
            )

        elif self.mode == "dynamic":
            kwargs: dict[str, Any] = {
                "headless": True,
                "network_idle": True,
                "timeout": int(self.timeout * 1000),  # milliseconds
                "useragent": self.user_agent,
            }

            if self.wait_selector:
                kwargs["wait_selector"] = self.wait_selector

            page = DynamicFetcher.fetch(url, **kwargs)

        else:
            kwargs = {
                "headless": True,
                "network_idle": True,
                "timeout": int(self.timeout * 1000),  # milliseconds
                "useragent": self.user_agent,
            }

            if self.wait_selector:
                kwargs["wait_selector"] = self.wait_selector

            page = StealthyFetcher.fetch(url, **kwargs)

        status = scrapling_response_status(page)
        text = scrapling_response_to_html(page)

        if status >= 400:
            raise FetchHTTPError(status)

        return status, text

# Backward-compatible name for older project files.
# This is NOT an httpx fallback. It points to ScraplingFetcher.
HttpFetcher = ScraplingFetcher

def make_fetcher(
    mode: str | None = None,
    wait_selector: Optional[str] = None,
) -> ScraplingFetcher:
    """
    Create the only supported fetcher: Scrapling.

    No engine argument exists because this project is intentionally
    Scrapling-only.

    Environment variable support:
    - SCRAPER_MODE=fast
    - SCRAPER_MODE=dynamic
    - SCRAPER_MODE=stealth

    Default:
    - stealth
    """
    selected_mode = (mode or os.environ.get("SCRAPER_MODE", "stealth")).strip().lower()

    return ScraplingFetcher(
        mode=selected_mode,
        wait_selector=wait_selector,
    )
