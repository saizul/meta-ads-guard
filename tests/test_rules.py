from datetime import timedelta

import pytest

from meta_ads_guard.errors import ConfigError, ExpressionError
from meta_ads_guard.marketing import AdObject
from meta_ads_guard.rules import (
    LabelAction,
    NotifyAction,
    PauseAction,
    ScaleBudgetAction,
    parse_action,
    parse_rule,
    split_trailing_window,
)


def rule(**raw):
    base = {"name": "r1", "when": "spend > 1", "actions": ["notify"]}
    base.update(raw)
    return parse_rule(base, 0, defaults={}, global_vars={})


def test_minimal_rule_defaults():
    r = rule()
    assert r.level == "adset"
    assert r.window == "3d"
    assert r.cooldown == timedelta(hours=6)
    assert r.filters.effective_status == ("ACTIVE",)
    assert r.result[0] == "omni_purchase"
    assert r.windows() == ["3d"]


def test_trailing_window_clause():
    r = rule(when="spend > 1500 BDT and purchases == 0 in last 3 days")
    assert r.window == "3d"
    assert r.condition.source == "spend > 1500 BDT and purchases == 0"
    assert split_trailing_window("ctr < 1 over last_7d") == ("ctr < 1", "last_7d")
    assert split_trailing_window("ctr < 1 during today") == ("ctr < 1", "today")
    assert split_trailing_window("ctr < 1") == ("ctr < 1", None)


def test_trailing_window_conflict():
    with pytest.raises(ConfigError, match="disagree"):
        rule(when="spend > 1 in 3d", window="7d")
    assert rule(when="spend > 1 in 7 days", window="7d").window == "7d"


def test_when_list_is_anded():
    r = rule(when=["spend > 10", "purchases == 0 or clicks < 5"])
    assert r.condition.source == "(spend > 10) and (purchases == 0 or clicks < 5)"


def test_windows_collects_explicit_windows():
    r = rule(when="spend[today] > 2 * spend[last_7d] / 7 and daily_budget > 500")
    assert r.windows() == ["3d", "last_7d", "today"]


def test_vars_and_globals():
    r = parse_rule({"name": "cpa", "when": "cpa > target_cpa × 1.4", "vars": {"target_cpa": 500},
                    "actions": ["pause"]}, 0, defaults={}, global_vars={"other": 1})
    assert r.vars == {"other": 1, "target_cpa": 500}
    r2 = parse_rule({"name": "g", "when": "cpa > Target_CPA", "actions": ["pause"]}, 0,
                    defaults={}, global_vars={"target_cpa": 300})
    assert r2.vars["target_cpa"] == 300


def test_unknown_metric_suggests():
    with pytest.raises(ExpressionError, match="did you mean 'spend'"):
        rule(when="spendd > 1")


def test_var_with_window_rejected():
    with pytest.raises(ExpressionError, match="cannot take a window"):
        rule(when="spend > target[3d]", vars={"target": 1})


def test_object_field_with_window_rejected():
    with pytest.raises(ExpressionError, match="object field"):
        rule(when="daily_budget[3d] > 1")


def test_bad_window_in_reference():
    with pytest.raises(ExpressionError, match="unknown window"):
        rule(when="spend[fortnight] > 1")


def test_var_cannot_shadow_metric():
    with pytest.raises(ConfigError, match="shadows"):
        rule(vars={"spend": 1})


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("pause", PauseAction()),
        ({"type": "pause"}, PauseAction()),
        ("notify", NotifyAction()),
        ({"notify": "telegram"}, NotifyAction(("telegram",))),
        ({"notify": {"channels": ["a", "b"], "message": "hi {name}"}},
         NotifyAction(("a", "b"), "hi {name}")),
        ({"scale_budget": 20}, ScaleBudgetAction(20)),
        ({"scale_budget": {"percent": -15, "min": 500}}, ScaleBudgetAction(-15, 500)),
        ({"scale-budget": {"percent": 10, "max": 9000, "budget": "daily"}},
         ScaleBudgetAction(10, None, 9000, "daily")),
        ({"label": "fatigued"}, LabelAction("fatigued")),
        ({"label": {"name": "x"}}, LabelAction("x")),
    ],
)
def test_parse_action(raw, expected):
    assert parse_action(raw, "a") == expected


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ("delete", "unknown action"),
        ({"scale_budget": {}}, "needs percent"),
        ({"scale_budget": {"percent": 0}}, "non-zero"),
        ({"scale_budget": {"percent": -100}}, "greater than -100"),
        ({"scale_budget": {"percent": 10, "min": 5, "max": 1}}, "greater than max"),
        ({"scale_budget": {"percent": 10, "min": -5}}, "negative"),
        ({"scale_budget": {"percent": 10, "budget": "weekly"}}, "budget must be"),
        ({"label": ""}, "needs a name"),
        ({"pause": 1, "notify": 2}, "single-key"),
        (["pause"], "single-key"),
        ({"scale_budget": {"percent": "lots"}}, "must be a number"),
    ],
)
def test_bad_actions(raw, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_action(raw, "a")


def test_action_descriptions():
    assert ScaleBudgetAction(15, None, 20000).describe() == "scale budget +15% (max 20000)"
    assert ScaleBudgetAction(-20, 500).describe() == "scale budget -20% (min 500)"
    assert NotifyAction(("tg",)).describe() == "notify (tg)"
    assert LabelAction("x").describe() == "label 'x'"


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"name": "bad name!"}, "name is required"),
        ({"level": "account"}, "level must be"),
        ({"when": ""}, "condition"),
        ({"when": []}, "empty"),
        ({"actions": []}, "actions are required"),
        ({"actions": None}, "actions are required"),
        ({"level": "ad", "actions": [{"scale_budget": 10}]}, "ads have no budget"),
        ({"actions": ["pause", "pause"]}, "once per rule"),
        ({"actions": ["pause", {"scale_budget": 10}]}, "makes no sense"),
        ({"color": "red"}, "unknown keys"),
        ({"enabled": "yes"}, "enabled must be"),
        ({"message": 5}, "message must be"),
        ({"vars": [1]}, "vars must be"),
        ({"filters": {"name_regex": "("}}, "invalid name_regex"),
        ({"filters": {"colour": 1}}, "unknown filter"),
        ({"filters": []}, "filters must be"),
    ],
)
def test_rule_errors(raw, fragment):
    with pytest.raises(ConfigError, match=fragment):
        rule(**raw)


def test_rule_must_be_mapping():
    with pytest.raises(ConfigError):
        parse_rule("pause", 0, defaults={}, global_vars={})


def test_actions_may_be_single_value():
    assert rule(actions="pause").actions == [PauseAction()]


def obj(**kw):
    base = {"id": "1", "level": "adset", "name": "BD | Retargeting | Video",
            "effective_status": "ACTIVE", "campaign_id": "c1", "labels": ["Winner"]}
    base.update(kw)
    return AdObject(**base)


def test_filters():
    f = rule(filters={"name_contains": ["retarget"], "labels_none": ["winner"]}).filters
    assert not f.matches(obj())
    f = rule(filters={"name_contains": "retarget", "labels_any": ["WINNER"]}).filters
    assert f.matches(obj())
    assert not f.matches(obj(effective_status="PAUSED"))
    f = rule(filters={"name_regex": r"\| video$", "campaign_ids": ["c1"]}).filters
    assert f.matches(obj())
    assert not f.matches(obj(campaign_id="c2"))
    f = rule(filters={"ids": ["1", "2"], "exclude_ids": "2"}).filters
    assert f.matches(obj()) and not f.matches(obj(id="2")) and not f.matches(obj(id="3"))
    f = rule(filters={"name_not_contains": "video", "effective_status": []}).filters
    assert not f.matches(obj())
    assert f.matches(obj(name="Static", effective_status="PAUSED"))
    f = rule(filters={"campaign_ids": "c9"}).filters
    assert f.matches(obj(level="campaign", id="c9", campaign_id=None))
