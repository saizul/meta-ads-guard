"""Lookback windows and durations.

Window syntax (case-insensitive):

``today``            the current day in the ad account time zone
``yesterday``        the previous full day
``3d`` / ``last 3 days``
                     rolling: today plus the 2 days before it (spend so far
                     today counts, which is what a guard rule usually wants)
``last_3d``          Meta preset semantics: the 3 full days *before* today
``this_month``       first of the month until today
``last_month``       the previous calendar month
``lifetime``         everything (``date_preset=maximum``)

Duration syntax for cooldowns: ``90s``, ``30m``, ``6h``, ``2d``, ``1w``, or
combinations like ``1h30m``. A bare number is seconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError


@dataclass(frozen=True)
class Window:
    """A resolved lookback window.

    ``key`` is the canonical spelling (``3d``, ``last_7d``, ``lifetime``) and
    is what metric references such as ``spend[3d]`` are matched against.
    """

    key: str
    since: Optional[date] = None
    until: Optional[date] = None
    preset: Optional[str] = None

    @property
    def days(self) -> int:
        if self.since is None or self.until is None:
            return 10_000
        return (self.until - self.since).days + 1

    def insights_params(self) -> dict[str, object]:
        if self.preset:
            return {"date_preset": self.preset}
        assert self.since and self.until
        return {"time_range": {"since": self.since.isoformat(), "until": self.until.isoformat()}}

    def label(self) -> str:
        if self.preset:
            return self.key
        assert self.since and self.until
        if self.since == self.until:
            return f"{self.key} ({self.since.isoformat()})"
        return f"{self.key} ({self.since.isoformat()}..{self.until.isoformat()})"


_ROLLING = re.compile(r"^(\d{1,3})\s*d(?:ays?)?$")
_HUMAN = re.compile(r"^(?:last|past)\s+(\d{1,3})\s+days?$")
_PRESET = re.compile(r"^last_(\d{1,3})d$")


def canonical_window(spec: str) -> str:
    """Normalise a window spelling to its canonical key, or raise ConfigError."""
    text = str(spec).strip().lower().replace("-", "_")
    if text in {"today", "yesterday", "this_month", "last_month", "lifetime", "maximum"}:
        return "lifetime" if text == "maximum" else text
    m = _ROLLING.match(text) or _HUMAN.match(text)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise ConfigError(f"window must be at least 1 day: {spec!r}")
        return f"{n}d"
    m = _PRESET.match(text)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise ConfigError(f"window must be at least 1 day: {spec!r}")
        return f"last_{n}d"
    raise ConfigError(
        f"unknown window {spec!r}; use today, yesterday, Nd, last_Nd, this_month, "
        "last_month or lifetime"
    )


def resolve_window(spec: str, today: date) -> Window:
    key = canonical_window(spec)
    if key == "today":
        return Window(key, today, today)
    if key == "yesterday":
        y = today - timedelta(days=1)
        return Window(key, y, y)
    if key == "this_month":
        return Window(key, today.replace(day=1), today)
    if key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return Window(key, last.replace(day=1), last)
    if key == "lifetime":
        return Window(key, preset="maximum")
    if key.startswith("last_"):
        n = int(key[5:-1])
        return Window(key, today - timedelta(days=n), today - timedelta(days=1))
    n = int(key[:-1])
    return Window(key, today - timedelta(days=n - 1), today)


_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: object) -> timedelta:
    """Parse ``6h``, ``1h30m``, ``2d`` or a number of seconds."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ConfigError(f"invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ConfigError(f"duration cannot be negative: {value!r}")
        return timedelta(seconds=float(value))
    text = str(value).strip().lower().replace(" ", "")
    if text in {"", "0", "none", "off"}:
        return timedelta(0)
    if text.isdigit():
        return timedelta(seconds=int(text))
    pos = 0
    total = 0.0
    for m in _DURATION.finditer(text):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(text) or pos == 0:
        raise ConfigError(f"invalid duration {value!r}; use e.g. 30m, 6h, 1d, 1h30m")
    return timedelta(seconds=total)


def format_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "0s"
    out = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            out.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(out)


def get_timezone(name: Optional[str]) -> tzinfo:
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown time zone {name!r}") from exc


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_graph_time(value: Optional[str]) -> Optional[datetime]:
    """Parse Graph timestamps such as ``2026-01-31T10:00:00+0600``."""
    if not value:
        return None
    text = value.strip()
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
