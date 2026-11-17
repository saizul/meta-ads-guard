"""Rule schedules: limit when a rule may act to certain days and hours.

A schedule is evaluated in the ad account time zone unless it names its own
``timezone``. Example::

    schedule:
      days: mon-fri            # or [mon, wed, fri], weekdays, weekends, daily
      hours: "09:00-21:00"     # or a list; "22:00-02:00" wraps past midnight
      exclude_dates: [2026-12-25]

``hours`` ranges include the start minute and exclude the end minute, so
``09:00-21:00`` is active from 09:00:00 up to 20:59:59. A range that wraps
past midnight belongs to the day it starts on: with ``days: fri`` and
``hours: "22:00-02:00"``, Saturday 01:30 is inside the schedule and Friday
01:30 is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Optional

from .errors import ConfigError
from .timeutil import get_timezone

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_ALIASES = {
    "monday": "mon", "tuesday": "tue", "tues": "tue", "wednesday": "wed",
    "thursday": "thu", "thur": "thu", "thurs": "thu", "friday": "fri",
    "saturday": "sat", "sunday": "sun",
}
_DAY_GROUPS = {
    "weekdays": DAY_NAMES[:5],
    "weekends": DAY_NAMES[5:],
    "weekend": DAY_NAMES[5:],
    "daily": DAY_NAMES,
    "every_day": DAY_NAMES,
    "everyday": DAY_NAMES,
    "all": DAY_NAMES,
}
_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_ALWAYS = {"always", "any", "none", "off"}
_KEYS = {"days", "hours", "timezone", "exclude_dates"}


@dataclass(frozen=True)
class HourRange:
    """Minutes since midnight; ``end`` is exclusive and may be 1440 (24:00)."""

    start: int
    end: int

    @property
    def wraps(self) -> bool:
        return self.end <= self.start

    def describe(self) -> str:
        return f"{_hhmm(self.start)}-{_hhmm(self.end)}"


@dataclass(frozen=True)
class Schedule:
    days: tuple[int, ...] = tuple(range(7))  # 0 = Monday, as in date.weekday()
    hours: tuple[HourRange, ...] = ()
    timezone: Optional[str] = None
    exclude_dates: tuple[date, ...] = ()

    def tz(self, default: tzinfo) -> tzinfo:
        return get_timezone(self.timezone) if self.timezone else default

    def is_active(self, now: datetime, default_tz: tzinfo) -> bool:
        """Whether ``now`` (an aware datetime) falls inside the schedule."""
        local = now.astimezone(self.tz(default_tz))
        if not self.hours:
            return self._day_ok(local.date())
        minute = local.hour * 60 + local.minute
        for rng in self.hours:
            if not rng.wraps:
                if rng.start <= minute < rng.end and self._day_ok(local.date()):
                    return True
                continue
            if minute >= rng.start and self._day_ok(local.date()):
                return True
            if minute < rng.end and self._day_ok(local.date() - timedelta(days=1)):
                return True
        return False

    def _day_ok(self, day: date) -> bool:
        return day.weekday() in self.days and day not in self.exclude_dates

    def describe(self) -> str:
        parts = [_describe_days(self.days)]
        if self.hours:
            parts.append(", ".join(r.describe() for r in self.hours))
        if self.exclude_dates:
            parts.append(f"except {len(self.exclude_dates)} date(s)")
        if self.timezone:
            parts.append(self.timezone)
        return " ".join(parts)

    def explain(self, now: datetime, default_tz: tzinfo) -> str:
        """Human-readable reason used when a rule is skipped."""
        tz = self.tz(default_tz)
        local = now.astimezone(tz)
        when = f"{DAY_NAMES[local.weekday()]} {local:%Y-%m-%d %H:%M}"
        zone = self.timezone or getattr(tz, "key", None) or str(tz)
        return f"outside schedule {self.describe()} (now {when} {zone})"


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _describe_days(days: tuple[int, ...]) -> str:
    if days == tuple(range(7)):
        return "daily"
    if days == tuple(range(5)):
        return "mon-fri"
    if days == (5, 6):
        return "sat-sun"
    return ",".join(DAY_NAMES[d] for d in days)


def _day_index(token: str, where: str) -> int:
    name = _DAY_ALIASES.get(token, token)
    if name not in DAY_NAMES:
        raise ConfigError(
            f"{where}: unknown day {token!r}; use mon..sun, weekdays, weekends or daily"
        )
    return DAY_NAMES.index(name)


def parse_days(raw: Any, where: str) -> tuple[int, ...]:
    if raw is None:
        return tuple(range(7))
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    out: set[int] = set()
    for item in items:
        token = str(item).strip().lower().replace(" ", "")
        if not token:
            continue
        if token in _DAY_GROUPS:
            out.update(DAY_NAMES.index(d) for d in _DAY_GROUPS[token])
            continue
        if "-" in token:
            first, _, last = token.partition("-")
            a, b = _day_index(first, where), _day_index(last, where)
            # Ranges may wrap the week: fri-mon is fri, sat, sun, mon.
            span = (b - a) % 7
            out.update((a + i) % 7 for i in range(span + 1))
            continue
        out.add(_day_index(token, where))
    if not out:
        raise ConfigError(f"{where}: days is empty")
    return tuple(sorted(out))


def parse_hours(raw: Any, where: str) -> tuple[HourRange, ...]:
    if raw is None:
        return ()
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    ranges = []
    for item in items:
        text = str(item).strip()
        m = _RANGE.match(text)
        if not m:
            raise ConfigError(f"{where}: hours must look like 09:00-21:00, got {item!r}")
        sh, sm, eh, em = (int(g) for g in m.groups())
        if sh > 23 or sm > 59 or em > 59 or eh > 24 or (eh == 24 and em):
            raise ConfigError(f"{where}: invalid time in {text!r}")
        start, end = sh * 60 + sm, eh * 60 + em
        if start == end or (start == 0 and end == 1440):
            raise ConfigError(
                f"{where}: {text!r} covers the whole day; leave hours out instead"
            )
        ranges.append(HourRange(start, end))
    if not ranges:
        raise ConfigError(f"{where}: hours is empty")
    return tuple(ranges)


def parse_dates(raw: Any, where: str) -> tuple[date, ...]:
    if raw is None:
        return ()
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out = []
    for item in items:
        if isinstance(item, datetime):
            out.append(item.date())
        elif isinstance(item, date):  # YAML turns 2026-12-25 into a date already
            out.append(item)
        else:
            try:
                out.append(date.fromisoformat(str(item).strip()))
            except ValueError as exc:
                raise ConfigError(
                    f"{where}: exclude_dates entries must be YYYY-MM-DD, got {item!r}"
                ) from exc
    return tuple(sorted(set(out)))


def parse_schedule(raw: Any, where: str) -> Optional[Schedule]:
    """Parse a ``schedule`` mapping. ``None``, ``false`` or ``always`` mean no schedule."""
    if raw is None or raw is False:
        return None
    if isinstance(raw, str) and raw.strip().lower() in _ALWAYS:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: schedule must be a mapping (days, hours, timezone)")
    unknown = set(raw) - _KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown schedule keys {sorted(unknown)}")
    if not raw:
        raise ConfigError(f"{where}: schedule is empty; set days and/or hours")
    zone = raw.get("timezone")
    if zone is not None:
        zone = str(zone)
        get_timezone(zone)  # raises ConfigError for unknown zones
    return Schedule(
        days=parse_days(raw.get("days"), f"{where}.days"),
        hours=parse_hours(raw.get("hours"), f"{where}.hours"),
        timezone=zone,
        exclude_dates=parse_dates(raw.get("exclude_dates"), f"{where}.exclude_dates"),
    )


__all__ = ["DAY_NAMES", "HourRange", "Schedule", "parse_schedule"]
