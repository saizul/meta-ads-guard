import io
import json
import textwrap
from datetime import date, datetime, timedelta, timezone

import pytest

from meta_ads_guard.cli import main
from meta_ads_guard.config import parse_config
from meta_ads_guard.errors import ConfigError
from meta_ads_guard.schedule import HourRange, Schedule, parse_schedule
from meta_ads_guard.timeutil import get_timezone

from .conftest import TOKEN
from .fakeaccount import FakeAccount
from .test_engine import KILL, engine

DHAKA = get_timezone("Asia/Dhaka")


def at(text, tz=DHAKA):
    """A local wall-clock time in Dhaka (or ``tz``) as an aware datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=tz)


# 2026-11-12 is a Thursday, 2026-11-13 a Friday, 2026-11-14 a Saturday.


def test_parse_days_forms():
    assert parse_schedule({"days": "mon-fri"}, "s").days == (0, 1, 2, 3, 4)
    assert parse_schedule({"days": "weekends"}, "s").days == (5, 6)
    assert parse_schedule({"days": ["Monday", "wed", "FRI"]}, "s").days == (0, 2, 4)
    assert parse_schedule({"days": "sun, tue"}, "s").days == (1, 6)
    assert parse_schedule({"days": "fri-mon"}, "s").days == (0, 4, 5, 6)
    assert parse_schedule({"days": "daily"}, "s").days == tuple(range(7))


def test_parse_hours_and_dates():
    s = parse_schedule(
        {
            "hours": ["09:00-12:30", "22:00-02:00"],
            "exclude_dates": ["2026-12-25", date(2026, 1, 1)],
        },
        "s",
    )
    assert s.hours == (HourRange(540, 750), HourRange(1320, 120))
    assert s.hours[1].wraps and not s.hours[0].wraps
    assert s.exclude_dates == (date(2026, 1, 1), date(2026, 12, 25))
    assert parse_schedule({"hours": "18:00-24:00"}, "s").hours == (HourRange(1080, 1440),)


@pytest.mark.parametrize("raw", [None, False, "always", "ALWAYS", "off"])
def test_no_schedule_spellings(raw):
    assert parse_schedule(raw, "s") is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"days": "funday"}, "unknown day"),
        ({"days": []}, "days is empty"),
        ({"hours": "9-17"}, "hours must look like"),
        ({"hours": "25:00-26:00"}, "invalid time"),
        ({"hours": "09:60-10:00"}, "invalid time"),
        ({"hours": "24:30-01:00"}, "invalid time"),
        ({"hours": "10:00-10:00"}, "whole day"),
        ({"hours": "00:00-24:00"}, "whole day"),
        ({"exclude_dates": ["25/12/2026"]}, "YYYY-MM-DD"),
        ({"timezone": "Mars/Olympus"}, "unknown time zone"),
        ({"day": "mon"}, "unknown schedule keys"),
        ({}, "schedule is empty"),
        ("weekdays", "must be a mapping"),
    ],
)
def test_invalid_schedules(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_schedule(raw, "rule 'x'.schedule")


def test_is_active_days_and_hours():
    s = parse_schedule({"days": "mon-fri", "hours": "09:00-21:00"}, "s")
    assert s.is_active(at("2026-11-12T09:00"), DHAKA)
    assert s.is_active(at("2026-11-12T20:59"), DHAKA)
    assert not s.is_active(at("2026-11-12T21:00"), DHAKA)  # end is exclusive
    assert not s.is_active(at("2026-11-12T08:59"), DHAKA)
    assert not s.is_active(at("2026-11-14T12:00"), DHAKA)  # Saturday
    # The same instant expressed in UTC is converted to the account time zone.
    assert s.is_active(datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc), DHAKA)


def test_wrapping_range_belongs_to_the_start_day():
    s = parse_schedule({"days": "fri", "hours": "22:00-02:00"}, "s")
    assert s.is_active(at("2026-11-13T23:30"), DHAKA)  # Friday night
    assert s.is_active(at("2026-11-14T01:30"), DHAKA)  # early Saturday, Friday's range
    assert not s.is_active(at("2026-11-13T01:30"), DHAKA)  # early Friday, Thursday's range
    assert not s.is_active(at("2026-11-14T22:30"), DHAKA)  # Saturday night


def test_exclude_dates_and_own_timezone():
    s = parse_schedule({"exclude_dates": ["2026-11-12"]}, "s")
    assert not s.is_active(at("2026-11-12T12:00"), DHAKA)
    assert s.is_active(at("2026-11-13T00:00"), DHAKA)
    london = parse_schedule({"hours": "09:00-17:00", "timezone": "Europe/London"}, "s")
    # 15:00 in Dhaka is 09:00 in London (GMT in November).
    assert london.is_active(at("2026-11-12T15:00"), DHAKA)
    assert not london.is_active(at("2026-11-12T14:59"), DHAKA)


def test_describe_and_explain():
    s = parse_schedule({"days": "mon-fri", "hours": "09:00-21:00"}, "s")
    assert s.describe() == "mon-fri 09:00-21:00"
    assert Schedule().describe() == "daily"
    reason = s.explain(at("2026-11-14T10:15"), DHAKA)
    assert reason == "outside schedule mon-fri 09:00-21:00 (now sat 2026-11-14 10:15 Asia/Dhaka)"


def _config(rules, **extra):
    doc = {"account": {"id": "act_1234567890", "timezone": "Asia/Dhaka"}, "rules": rules}
    doc.update(extra)
    return parse_config(doc, env={})


def test_defaults_schedule_and_rule_override():
    cfg = _config(
        [KILL, {**KILL, "name": "always-on", "schedule": "always"},
         {**KILL, "name": "weekend", "schedule": {"days": "weekends"}}],
        defaults={"schedule": {"days": "weekdays", "hours": "08:00-20:00"}},
    )
    by_name = {r.name: r for r in cfg.rules}
    assert by_name["kill"].schedule.describe() == "mon-fri 08:00-20:00"
    assert by_name["always-on"].schedule is None
    assert by_name["weekend"].schedule.describe() == "sat-sun"


def test_invalid_defaults_schedule_is_reported():
    with pytest.raises(ConfigError, match=r"defaults\.schedule"):
        _config([KILL], defaults={"schedule": {"hours": "late"}})


@pytest.fixture
def account(transport):
    acc = FakeAccount(transport)
    acc.add_adset("101", "BD | Cold | Video", daily_budget=150000)
    acc.insights("adset", "*", "101", spend=1650.5, impressions=40000, clicks=300, reach=15000)
    return acc


# test_engine.NOW is Thursday 2026-11-12 06:00 in Dhaka.


def test_engine_skips_rule_outside_schedule(api, account):
    rule = {**KILL, "schedule": {"days": "mon-fri", "hours": "09:00-21:00"}}
    report = engine(_config([rule]), api, apply=True).run()
    assert report.matches == []
    assert report.checked == {}
    assert report.skipped_rules == {
        "kill": "outside schedule mon-fri 09:00-21:00 (now thu 2026-11-12 06:00 Asia/Dhaka)"
    }
    assert account.mutations == []
    assert report.to_dict()["skipped_rules"] == report.skipped_rules


def test_engine_runs_rule_inside_schedule(api, account):
    rule = {**KILL, "schedule": {"days": "thu", "hours": "05:00-07:00"}}
    report = engine(_config([rule]), api, apply=True).run()
    assert [m.obj.id for m in report.matches] == ["101"]
    assert report.skipped_rules == {}
    assert account.mutations


def test_engine_ignore_schedule(api, account):
    rule = {**KILL, "schedule": {"days": "weekends"}}
    report = engine(_config([rule]), api, ignore_schedule=True).run()
    assert [m.obj.id for m in report.matches] == ["101"]
    assert report.skipped_rules == {}


def test_engine_schedule_uses_account_timezone_from_api(api, account):
    # No account.timezone in the config: the ad account reports Asia/Dhaka.
    cfg = parse_config(
        {"account": {"id": "act_1234567890"},
         "rules": [{**KILL, "schedule": {"hours": "05:00-07:00"}}]},
        env={},
    )
    report = engine(cfg, api).run()
    assert [m.obj.id for m in report.matches] == ["101"]


CLI_CONFIG = """
account:
  id: act_1234567890
  currency: BDT
  timezone: Asia/Dhaka
rules:
  - name: kill-no-purchase
    when: spend > 1500 BDT and purchases == 0 in last 3 days
    actions: [pause]
    schedule:
      days: daily
      exclude_dates: [{dates}]
"""


@pytest.fixture
def cli_cfg(tmp_path):
    # Excluding yesterday, today and tomorrow makes the rule inactive whenever the test runs.
    today = datetime.now(timezone.utc).date()
    dates = ", ".join((today + timedelta(days=d)).isoformat() for d in (-1, 0, 1))
    path = tmp_path / "guard.yaml"
    path.write_text(textwrap.dedent(CLI_CONFIG).format(dates=dates), encoding="utf-8")
    return path


def _cli(argv, transport=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, env={"META_ACCESS_TOKEN": TOKEN}, transport=transport, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_validate_shows_schedule(cli_cfg):
    code, out, _ = _cli(["validate", "-c", str(cli_cfg)])
    assert code == 0
    assert "schedule" in out and "daily except 3 date(s)" in out


def test_cli_run_reports_skipped_rule_and_ignore_flag(cli_cfg, transport, account):
    code, out, _ = _cli(["run", "-c", str(cli_cfg)], transport)
    assert code == 0
    assert "rules not run: kill-no-purchase outside schedule" in out
    assert "no rule ran (all are outside their schedule)" in out

    code, out, _ = _cli(["run", "-c", str(cli_cfg), "--ignore-schedule", "--json"], transport)
    assert code == 0
    data = json.loads(out)
    assert data["skipped_rules"] == {}
    assert [m["object_id"] for m in data["matches"]] == ["101"]
