"""Rate-limit awareness for the Marketing API.

Meta reports how close an app is to its limits in response headers:

``X-Business-Use-Case-Usage``
    JSON object keyed by business object id. Each value is a list of
    ``{"type", "call_count", "total_cputime", "total_time",
    "estimated_time_to_regain_access", "ads_api_access_tier"}`` entries. The
    three usage numbers are percentages, ``estimated_time_to_regain_access``
    is in **minutes**.
``X-Ad-Account-Usage``
    ``{"acc_id_util_pct", "reset_time_duration", "ads_api_access_tier"}``;
    ``reset_time_duration`` is in seconds.
``X-FB-Ads-Insights-Throttle``
    ``{"app_id_util_pct", "acc_id_util_pct", "ads_api_access_tier"}``.
``X-App-Usage``
    ``{"call_count", "total_time", "total_cputime"}`` (platform limits).

:class:`RateLimiter` reads all of them after every response and sleeps before
the next call when usage crosses a threshold, and computes the wait before a
retry when the API answers with a throttling error.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Throttling error codes. 4 = app limit, 17 = user/ad account limit,
# 32 = page limit, 613 = custom rate limit, 80000-80014 = Business Use Case.
THROTTLE_CODES = frozenset({4, 17, 32, 613, *range(80000, 80015)})

# Throttling subcodes where retrying soon cannot succeed: the limit is a daily
# or hourly quota on a specific kind of change, not on call volume.
NON_RETRYABLE_SUBCODES = frozenset(
    {
        1885172,  # 17: spend limit changed more than 10 times per day
        1487632,  # 613: ad set budget changed more than 4 times per hour
        1487225,  # 613: ad creation limit tied to daily spend limit
    }
)

# Transient server-side errors worth retrying.
TRANSIENT_CODES = frozenset({1, 2})

USAGE_HEADERS = (
    "x-business-use-case-usage",
    "x-ad-account-usage",
    "x-fb-ads-insights-throttle",
    "x-app-usage",
)


@dataclass
class UsageSnapshot:
    """Highest usage percentage seen in one response, plus the wait hint."""

    max_pct: float = 0.0
    regain_seconds: float = 0.0
    tier: Optional[str] = None
    sources: dict[str, float] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.sources and self.regain_seconds == 0


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _load(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        log.debug("ignoring unparsable usage header: %r", raw[:200])
        return None


def parse_usage(headers: dict[str, str]) -> UsageSnapshot:
    """Combine every usage header in ``headers`` into one snapshot."""
    lowered = {k.lower(): v for k, v in headers.items()}
    snap = UsageSnapshot()

    def note(source: str, pct: float) -> None:
        snap.sources[source] = max(snap.sources.get(source, 0.0), pct)
        snap.max_pct = max(snap.max_pct, pct)

    buc = _load(lowered.get("x-business-use-case-usage"))
    if isinstance(buc, dict):
        for business_id, entries in buc.items():
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type", "unknown")
                pct = max(
                    _num(entry.get("call_count")),
                    _num(entry.get("total_cputime")),
                    _num(entry.get("total_time")),
                )
                note(f"buc:{business_id}:{kind}", pct)
                regain = _num(entry.get("estimated_time_to_regain_access")) * 60.0
                snap.regain_seconds = max(snap.regain_seconds, regain)
                if entry.get("ads_api_access_tier"):
                    snap.tier = entry["ads_api_access_tier"]

    acc = _load(lowered.get("x-ad-account-usage"))
    if isinstance(acc, dict):
        pct = _num(acc.get("acc_id_util_pct"))
        note("ad_account", pct)
        if pct >= 100:
            snap.regain_seconds = max(snap.regain_seconds, _num(acc.get("reset_time_duration")))
        if acc.get("ads_api_access_tier"):
            snap.tier = acc["ads_api_access_tier"]

    ins = _load(lowered.get("x-fb-ads-insights-throttle"))
    if isinstance(ins, dict):
        note("insights_app", _num(ins.get("app_id_util_pct")))
        note("insights_account", _num(ins.get("acc_id_util_pct")))
        if ins.get("ads_api_access_tier"):
            snap.tier = ins["ads_api_access_tier"]

    app = _load(lowered.get("x-app-usage"))
    if isinstance(app, dict):
        note(
            "app",
            max(
                _num(app.get("call_count")),
                _num(app.get("total_time")),
                _num(app.get("total_cputime")),
            ),
        )
    return snap


class RateLimiter:
    """Decides how long to pause between and before retried calls.

    ``threshold`` is the usage percentage at which the client starts slowing
    down. Above it the pause grows linearly from ``base_pause`` up to
    ``base_pause * 6`` at 100 %. An explicit ``estimated_time_to_regain_access``
    always wins. Every wait is capped at ``max_wait`` seconds.
    """

    def __init__(
        self,
        *,
        threshold: float = 85.0,
        base_pause: float = 10.0,
        max_wait: float = 900.0,
        backoff_base: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        if not 0 < threshold <= 100:
            raise ValueError("threshold must be in (0, 100]")
        self.threshold = threshold
        self.base_pause = base_pause
        self.max_wait = max_wait
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._jitter = jitter
        self.last: UsageSnapshot = UsageSnapshot()
        self.total_slept = 0.0

    def pause_for(self, snap: UsageSnapshot) -> float:
        if snap.regain_seconds > 0:
            return min(snap.regain_seconds, self.max_wait)
        if snap.max_pct < self.threshold:
            return 0.0
        span = max(1.0, 100.0 - self.threshold)
        factor = 1.0 + 5.0 * min(1.0, (snap.max_pct - self.threshold) / span)
        return min(self.base_pause * factor, self.max_wait)

    def after_response(self, headers: dict[str, str]) -> float:
        """Record usage from ``headers`` and sleep if needed. Returns the pause."""
        snap = parse_usage(headers)
        if snap.empty:
            return 0.0
        self.last = snap
        wait = self.pause_for(snap)
        if wait > 0:
            log.warning(
                "API usage at %.0f%% (tier %s); pausing %.1fs", snap.max_pct, snap.tier, wait
            )
            self.sleep(wait)
        return wait

    def retry_delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """Delay before retry number ``attempt`` (0-based) after a throttling error."""
        if retry_after and retry_after > 0:
            return min(retry_after, self.max_wait)
        delay = self.backoff_base * (2**attempt)
        delay += delay * 0.25 * self._jitter()
        return min(delay, self.max_wait)

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.total_slept += seconds
        self._sleep(seconds)


def is_throttle(code: Optional[int], subcode: Optional[int] = None) -> bool:
    return code in THROTTLE_CODES


def is_retryable_throttle(code: Optional[int], subcode: Optional[int] = None) -> bool:
    return is_throttle(code, subcode) and subcode not in NON_RETRYABLE_SUBCODES
