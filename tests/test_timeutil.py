from datetime import date, timedelta, timezone

import pytest

from meta_ads_guard.errors import ConfigError
from meta_ads_guard.timeutil import (
    canonical_window,
    format_duration,
    get_timezone,
    parse_duration,
    parse_graph_time,
    resolve_window,
)

TODAY = date(2026, 11, 12)


@pytest.mark.parametrize(
    "spec, key",
    [
        ("3d", "3d"),
        ("3 days", "3d"),
        ("last 3 days", "3d"),
        ("past 1 day", "3d".replace("3", "1")),
        ("LAST_7D", "last_7d"),
        ("last-7d", "last_7d"),
        ("today", "today"),
        ("Yesterday", "yesterday"),
        ("maximum", "lifetime"),
        ("this_month", "this_month"),
    ],
)
def test_canonical_window(spec, key):
    assert canonical_window(spec) == key


@pytest.mark.parametrize("spec", ["0d", "last_0d", "fortnight", "3 weeks", ""])
def test_bad_windows(spec):
    with pytest.raises(ConfigError):
        canonical_window(spec)


def test_rolling_window_includes_today():
    w = resolve_window("3d", TODAY)
    assert (w.since, w.until) == (date(2026, 11, 10), TODAY)
    assert w.days == 3
    assert w.insights_params() == {"time_range": {"since": "2026-11-10", "until": "2026-11-12"}}


def test_preset_style_window_excludes_today():
    w = resolve_window("last_7d", TODAY)
    assert (w.since, w.until) == (date(2026, 11, 5), date(2026, 11, 11))
    assert w.days == 7


def test_named_windows():
    assert resolve_window("today", TODAY).since == TODAY
    y = resolve_window("yesterday", TODAY)
    assert y.since == y.until == TODAY - timedelta(days=1)
    m = resolve_window("this_month", TODAY)
    assert m.since == date(2026, 11, 1)
    lm = resolve_window("last_month", date(2026, 3, 15))
    assert (lm.since, lm.until) == (date(2026, 2, 1), date(2026, 2, 28))
    life = resolve_window("lifetime", TODAY)
    assert life.insights_params() == {"date_preset": "maximum"}
    assert life.days > 1000
    assert "2026-11-12" in resolve_window("today", TODAY).label()


@pytest.mark.parametrize(
    "value, seconds",
    [("6h", 21600), ("30m", 1800), ("1h30m", 5400), ("2d", 172800), ("1w", 604800),
     ("90s", 90), (120, 120), ("0", 0), ("off", 0), ("45", 45), ("1.5h", 5400)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value).total_seconds() == seconds


@pytest.mark.parametrize("value", ["6x", "h6", "1h 30", -5, True, "abc"])
def test_bad_durations(value):
    with pytest.raises(ConfigError):
        parse_duration(value)


def test_format_duration():
    assert format_duration(timedelta(hours=5, minutes=3)) == "5h3m"
    assert format_duration(timedelta(0)) == "0s"
    assert format_duration(timedelta(days=1, seconds=1)) == "1d1s"


def test_timezones():
    assert get_timezone(None) is timezone.utc
    assert str(get_timezone("Asia/Dhaka")) == "Asia/Dhaka"
    with pytest.raises(ConfigError):
        get_timezone("Mars/Olympus")


def test_parse_graph_time():
    dt = parse_graph_time("2026-11-01T10:00:00+0600")
    assert dt is not None and dt.utcoffset() == timedelta(hours=6)
    assert parse_graph_time("2026-11-01T10:00:00Z").utcoffset() == timedelta(0)
    assert parse_graph_time(None) is None
    assert parse_graph_time("garbage") is None
