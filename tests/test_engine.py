from datetime import datetime, timedelta, timezone

import pytest

from meta_ads_guard.audit import AuditLog
from meta_ads_guard.config import parse_config
from meta_ads_guard.engine import Engine, pretty_number
from meta_ads_guard.errors import ConfigError
from meta_ads_guard.state import MemoryState

from .conftest import TOKEN, graph_error
from .fakeaccount import FakeAccount

NOW = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)  # 06:00 in Dhaka
D3 = "2026-11-10"  # start of the 3d window
D7 = "2026-11-06"  # start of the 7d window


class Recorder:
    def __init__(self, name="tg"):
        self.name = name
        self.sent = []

    def send(self, subject, text):
        self.sent.append((subject, text))


def config(rules, **extra):
    doc = {"account": {"id": "act_1234567890", "timezone": "Asia/Dhaka"}, "rules": rules,
           "notifiers": {"tg": {"type": "telegram", "bot_token_env": "TG", "chat_id": "1"}}}
    doc.update(extra)
    return parse_config(doc, env={})


@pytest.fixture
def account(transport):
    acc = FakeAccount(transport)
    acc.add_adset("101", "BD | Cold | Video", daily_budget=150000)
    acc.add_adset("102", "BD | Retargeting", daily_budget=200000)
    acc.add_adset("103", "BD | Winner", daily_budget=300000, labels=["scaled"])
    acc.insights("adset", D3, "101", spend=1650.5, impressions=40000, clicks=300, reach=15000)
    acc.insights("adset", D3, "102", spend=2200, impressions=30000, clicks=450, reach=12000,
                 actions={"omni_purchase": 2, "purchase": 2}, action_values={"omni_purchase": 3000})
    acc.insights("adset", D3, "103", spend=5000, impressions=90000, clicks=1800, reach=20000,
                 actions={"omni_purchase": 20}, action_values={"omni_purchase": 25000})
    return acc


def engine(cfg, api, *, apply=False, state=None, notifiers=None, now=NOW, **kw):
    return Engine(cfg, api, state=state or MemoryState(), audit=AuditLog(None),
                  notifiers=notifiers if notifiers is not None else {"tg": Recorder()},
                  apply=apply, now=lambda: now, **kw)


KILL = {"name": "kill", "when": "spend > 1500 BDT and purchases == 0 in last 3 days",
        "actions": ["pause", "notify"], "cooldown": "12h"}


def test_dry_run_plans_but_changes_nothing(api, account):
    tg = Recorder()
    report = engine(config([KILL]), api, notifiers={"tg": tg}).run()
    assert report.dry_run
    assert report.account_name == "Demo Shop BD" and report.currency == "BDT"
    assert [m.obj.id for m in report.matches] == ["101"]
    statuses = [(r.kind, r.status) for r in report.matches[0].results]
    assert statuses == [("pause", "planned"), ("notify", "planned")]
    assert account.mutations == []
    assert tg.sent == []
    assert report.checked == {"kill": 3}
    assert dict(report.matches[0].values) == {"spend": 1650.5, "purchases": 0}


def test_apply_pauses_and_notifies(api, account):
    tg = Recorder()
    state = MemoryState()
    report = engine(config([KILL]), api, apply=True, state=state, notifiers={"tg": tg}).run()
    assert account.mutations == [("101", {"status": "PAUSED", "access_token": TOKEN})]
    assert report.matches[0].results[0].status == "applied"
    assert len(tg.sent) == 1
    subject, text = tg.sent[0]
    assert subject == "1 alert(s) on Demo Shop BD"
    assert "BD | Cold | Video" in text and "Spend 1,650.50 BDT" in text
    assert "pause [applied]" in text
    assert state.last_action("kill|101") == NOW
    assert report.ok


def test_cooldown_blocks_second_run(api, account):
    state = MemoryState()
    engine(config([KILL]), api, apply=True, state=state).run()
    account.objects["adset"][0]["status"] = "ACTIVE"  # someone switched it back on
    report = engine(config([KILL]), api, apply=True, state=state,
                    now=NOW + timedelta(hours=11)).run()
    assert report.matches[0].status == "cooldown"
    assert "1h left" in report.matches[0].detail
    assert len(account.mutations) == 1
    report = engine(config([KILL]), api, apply=True, state=state,
                    now=NOW + timedelta(hours=13)).run()
    assert report.matches[0].results[0].status == "applied"
    assert len(account.mutations) == 2


def test_dry_run_does_not_record_cooldown(api, account):
    state = MemoryState()
    engine(config([KILL]), api, state=state).run()
    assert state.entries() == []


def test_object_cooldown_scope_spans_rules(api, account):
    rules = [
        {"name": "a", "when": "spend > 1000", "actions": [{"label": "seen"}]},
        {"name": "b", "when": "spend > 1000", "actions": [{"label": "seen-twice"}]},
    ]
    cfg = config(rules, state={"cooldown_scope": "object"})
    report = engine(cfg, api, apply=True).run()
    by_rule = {(m.rule.name, m.obj.id): m.status for m in report.matches}
    assert by_rule[("a", "101")] == "acted"
    assert by_rule[("b", "101")] == "cooldown"


def test_cpa_rule_with_vars_and_budget_cut(api, account):
    rule = {"name": "cpa-high", "when": "cpa > target_cpa × 1.4 and results >= 2",
            "vars": {"target_cpa": 700}, "actions": [{"scale_budget": {"percent": -20,
                                                                        "min": 1700}}]}
    report = engine(config([rule]), api, apply=True).run()
    # 102: CPA 1100 > 980 -> cut 2000 by 20 % = 1600, held at min 1700
    assert [m.obj.id for m in report.matches] == ["102"]
    result = report.matches[0].results[0]
    assert result.status == "applied"
    assert "2,000.00 BDT -> 1,700.00 BDT (held at min)" in result.detail
    assert account.mutations[0][1]["daily_budget"] == 170000


def test_scale_up_with_cap_and_skip_when_at_cap(api, account):
    rule = {"name": "scale", "when": "roas > 3 and spend > 2000",
            "actions": [{"scale_budget": {"percent": 15, "max": 3300}}], "cooldown": "24h"}
    report = engine(config([rule]), api, apply=True).run()
    res = report.matches[0].results[0]
    assert report.matches[0].obj.id == "103"
    assert res.before == 3000 and res.after == 3300
    assert "(capped at max)" in res.detail
    account.objects["adset"][2]["daily_budget"] = "330000"
    report = engine(config([rule]), api, apply=True).run()
    assert report.matches[0].results[0].status == "skipped"
    assert len(account.mutations) == 1


def test_scale_skips_objects_without_budget(api, account):
    account.objects["adset"][0].pop("daily_budget")
    rule = {"name": "s", "when": "spend > 1500", "actions": [{"scale_budget": -10}]}
    report = engine(config([rule]), api).run()
    first = next(m for m in report.matches if m.obj.id == "101")
    assert first.results[0].status == "skipped"
    assert "campaign" in first.results[0].detail


def test_label_and_skip_existing(api, account):
    rule = {"name": "lbl", "when": "spend > 1000", "actions": [{"label": "scaled"}]}
    report = engine(config([rule]), api, apply=True).run()
    results = {m.obj.id: m.results[0].status for m in report.matches}
    assert results == {"101": "applied", "102": "applied", "103": "skipped"}
    assert account.mutations[0][1]["adlabels"] == [{"name": "scaled"}]


def test_paused_object_is_not_scaled_by_later_rule(api, account):
    rules = [KILL, {"name": "scale-all", "when": "spend > 1000",
                    "actions": [{"scale_budget": {"percent": 10, "max": 99999}}]}]
    report = engine(config(rules), api).run()
    scaled = [m.obj.id for m in report.matches if m.rule.name == "scale-all"]
    assert "101" not in scaled  # paused (planned) by the first rule


def test_max_actions_per_run(api, account):
    rule = {"name": "lbl", "when": "spend > 0", "actions": [{"label": "x"}]}
    report = engine(config([rule], safety={"max_actions_per_run": 2}), api).run()
    assert [m.status for m in report.matches] == ["acted", "acted", "capped"]


def test_frequency_rule_on_ads_with_window_suffix(api, account):
    account.add_ad("201", "Video A")
    account.add_ad("202", "Video B")
    account.insights("ad", D7, "201", impressions=9000, reach=2000, frequency=4.5)
    account.insights("ad", D7, "202", impressions=9000, reach=6000, frequency=1.5)
    rule = {"name": "fatigue", "level": "ad",
            "when": "frequency[7d] > 3.5 and impressions[7d] > 5000",
            "actions": ["notify", {"label": "fatigued"}]}
    report = engine(config([rule]), api).run()
    assert [m.obj.id for m in report.matches] == ["201"]
    assert dict(report.matches[0].values)["frequency[7d]"] == 4.5


def test_object_fields_in_conditions(api, account):
    rule = {"name": "big-budget", "when": "daily_budget >= 2000 and age_days > 5",
            "actions": ["notify"]}
    report = engine(config([rule]), api).run()
    assert {m.obj.id for m in report.matches} == {"102", "103"}


def test_raw_action_reference(api, account):
    rule = {"name": "raw", "when": "actions.omni_purchase >= 20", "actions": ["notify"]}
    report = engine(config([rule]), api).run()
    assert [m.obj.id for m in report.matches] == ["103"]


def test_filters_limit_objects(api, account):
    rule = {**KILL, "filters": {"name_contains": "retarget"}}
    report = engine(config([rule]), api).run()
    assert report.matches == [] and report.checked == {"kill": 1}


def test_rule_selection_and_disabled(api, account):
    rules = [KILL, {"name": "off", "when": "spend > 0", "enabled": False,
                    "actions": ["notify"]}]
    assert [m.rule.name for m in engine(config(rules), api).run().matches] == ["kill"]
    assert engine(config(rules), api).run(["off"]).checked == {"off": 3}
    with pytest.raises(ConfigError):
        engine(config(rules), api).run(["missing"])


def test_currency_mismatch_detected(api, account):
    account.currency = "USD"
    with pytest.raises(ConfigError, match="USD"):
        engine(config([KILL]), api).run()


def test_configured_currency_must_match_account(api, account):
    cfg = config([{"name": "x", "when": "spend > 1", "actions": ["notify"]}],
                 account={"id": "act_1234567890", "currency": "INR"})
    with pytest.raises(ConfigError, match="reports BDT"):
        engine(cfg, api).run()


def test_failed_action_is_reported_and_notification_still_sent(api, account):
    account.fail_mutation = graph_error(100, "Invalid parameter")
    tg = Recorder()
    report = engine(config([KILL]), api, apply=True, notifiers={"tg": tg}).run()
    assert report.matches[0].results[0].status == "failed"
    assert not report.ok and "Invalid parameter" in report.errors[0]
    assert "pause [failed]" in tg.sent[0][1]


def test_notify_in_dry_run_marks_subject(api, account):
    tg = Recorder()
    engine(config([KILL]), api, notifiers={"tg": tg}, notify_in_dry_run=True).run()
    assert tg.sent[0][0].startswith("[DRY RUN]")


def test_notify_each_mode_and_custom_message(api, account):
    rule = {"name": "all", "when": "spend > 1000",
            "message": "{name}: spend {spend:,.2f} {currency}, cpa {cpa}, target {target}",
            "vars": {"target": 500}, "actions": ["notify"]}
    tg = Recorder()
    engine(config([rule], notify={"mode": "each"}), api, apply=True, notifiers={"tg": tg}).run()
    assert len(tg.sent) == 3
    texts = [t for _, t in tg.sent]
    assert "- BD | Cold | Video: spend 1,650.50 BDT, cpa n/a, target 500" in texts[0]
    assert "cpa 1,100" in texts[1]


def test_bad_template_does_not_crash(api, account):
    rule = {"name": "t", "when": "spend > 1600", "message": "{spend:%%%}",
            "actions": ["notify"]}
    tg = Recorder()
    engine(config([rule]), api, apply=True, notifiers={"tg": tg}).run()
    assert "invalid message template" in tg.sent[0][1]


def test_notifier_failure_is_an_error_not_a_crash(api, account):
    class Broken:
        name = "tg"

        def send(self, s, t):
            from meta_ads_guard.notify import NotifyError
            raise NotifyError("boom")

    report = engine(config([KILL]), api, apply=True, notifiers={"tg": Broken()}).run()
    assert any("boom" in e for e in report.errors)


def test_channel_specific_notify(api, account):
    cfg = parse_config({
        "account": {"id": "act_1234567890"},
        "notifiers": {"tg": {"type": "telegram", "bot_token_env": "T", "chat_id": "1"},
                      "slack": {"type": "webhook", "url_env": "H"}},
        "rules": [{"name": "k", "when": "spend > 1600", "actions": [{"notify": "slack"}]}],
    }, env={})
    tg, slack = Recorder("tg"), Recorder("slack")
    engine(cfg, api, apply=True, notifiers={"tg": tg, "slack": slack}).run()
    assert tg.sent == [] and len(slack.sent) == 1


def test_insights_are_fetched_once_per_level_and_window(api, account, transport):
    rules = [KILL, {"name": "k2", "when": "spend > 1", "actions": ["notify"]}]
    engine(config(rules), api).run()
    assert len(transport.find("GET", r"/insights$")) == 1
    assert len(transport.find("GET", r"/adsets$")) == 1


def test_report_dict(api, account):
    report = engine(config([KILL]), api).run()
    d = report.to_dict()
    assert d["dry_run"] is True
    assert d["matches"][0]["object_id"] == "101"
    assert d["counts"] == {"planned": 2}
    assert d["api_calls"] >= 3


def test_audit_entries(api, account, tmp_path):
    path = tmp_path / "audit.jsonl"
    eng = Engine(config([KILL]), api, audit=AuditLog(str(path)), apply=True,
                 notifiers={"tg": Recorder()}, now=lambda: NOW)
    eng.run()
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert '"action": "pause"' in lines[0] and '"dry_run": false' in lines[0]


def test_pretty_number():
    assert pretty_number(1500.0) == "1,500"
    assert pretty_number(2.456) == "2.46"
    assert pretty_number(None) == "n/a"



def test_windows_follow_account_time_zone(api, account, transport):
    # 19:00 UTC on the 11th is already 01:00 on the 12th in Dhaka.
    late = datetime(2026, 11, 11, 19, 0, tzinfo=timezone.utc)
    eng = Engine(config([KILL]), api, now=lambda: late, notifiers={})
    report = eng.run()
    assert [m.obj.id for m in report.matches] == ["101"]
    call = transport.find("GET", r"/insights$")[0]
    assert call.params["time_range"] == {"since": D3, "until": "2026-11-12"}


def test_time_zone_taken_from_account_when_not_configured(api, account, transport):
    cfg = parse_config({"account": {"id": "act_1234567890"}, "rules": [KILL]}, env={})
    late = datetime(2026, 11, 11, 19, 0, tzinfo=timezone.utc)
    report = Engine(cfg, api, now=lambda: late).run()
    assert report.timezone == "Asia/Dhaka"
    assert transport.find("GET", r"/insights$")[0].params["time_range"]["until"] == "2026-11-12"
