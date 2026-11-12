import pytest

from meta_ads_guard.metrics import (
    InsightTotals,
    action_metric,
    compute_metrics,
    group_rows,
    is_known_metric,
    resolve_result_types,
)

from .conftest import insights_row

PURCHASES = resolve_result_types("purchases")


def totals(**kw):
    rows = group_rows([insights_row("adset", "1", **kw)], "adset")
    return rows["1"]


def test_basic_ratios_are_computed_from_counters():
    m = compute_metrics(totals(spend=1000, impressions=50000, clicks=500, reach=20000),
                        PURCHASES)
    assert m["spend"] == 1000
    assert m["ctr"] == pytest.approx(1.0)
    assert m["cpc"] == pytest.approx(2.0)
    assert m["cpm"] == pytest.approx(20.0)
    assert m["frequency"] == pytest.approx(2.5)


def test_reported_frequency_is_preferred_for_single_row():
    m = compute_metrics(totals(impressions=100, reach=50, frequency=2.2), PURCHASES)
    assert m["frequency"] == pytest.approx(2.2)


def test_purchase_types_are_not_double_counted():
    t = totals(
        spend=3000,
        actions={"omni_purchase": 3, "purchase": 3, "offsite_conversion.fb_pixel_purchase": 3},
        action_values={"omni_purchase": 9000, "purchase": 9000},
    )
    m = compute_metrics(t, PURCHASES)
    assert m["purchases"] == 3
    assert m["results"] == 3
    assert m["cpa"] == pytest.approx(1000)
    assert m["purchase_value"] == 9000
    assert m["roas"] == pytest.approx(3.0)


def test_falls_back_to_pixel_purchase():
    m = compute_metrics(totals(spend=500, actions={"offsite_conversion.fb_pixel_purchase": 2}),
                        PURCHASES)
    assert m["purchases"] == 2
    assert m["cost_per_purchase"] == 250


def test_zero_results_means_missing_cpa():
    m = compute_metrics(totals(spend=1600), PURCHASES)
    assert m["purchases"] == 0
    assert m["cpa"] is None
    assert m["roas"] is None or m["roas"] == 0


def test_no_row_means_zero_delivery():
    m = compute_metrics(None, PURCHASES)
    assert m["spend"] == 0 and m["impressions"] == 0
    assert m["ctr"] is None and m["frequency"] is None


def test_leads_and_messages_results():
    t = totals(spend=1200, actions={"onsite_conversion.messaging_conversation_started_7d": 12,
                                    "lead": 4})
    m = compute_metrics(t, resolve_result_types("messages"))
    assert m["results"] == 12
    assert m["cost_per_result"] == 100
    assert m["leads"] == 4
    assert m["cost_per_lead"] == 300


def test_custom_action_result():
    assert resolve_result_types("action:onsite_conversion.flow_complete") == (
        "onsite_conversion.flow_complete",)
    assert resolve_result_types(["a", "b"]) == ("a", "b")
    assert resolve_result_types("actions.link_click") == ("link_click",)


def test_multiple_rows_are_summed_and_ratios_recomputed():
    rows = [
        insights_row("ad", "9", spend=100, impressions=1000, clicks=10, reach=800, frequency=1.25,
                     actions={"purchase": 1}),
        insights_row("ad", "9", spend=300, impressions=3000, clicks=20, reach=1200,
                     frequency=2.5, actions={"purchase": 2}),
    ]
    t = group_rows(rows, "ad")["9"]
    assert t.rows == 2
    m = compute_metrics(t, PURCHASES)
    assert m["spend"] == 400
    assert m["ctr"] == pytest.approx(0.75)
    assert m["frequency"] == pytest.approx(2.0)  # 4000 / 2000, not a reported value
    assert m["purchases"] == 3
    assert m["days_with_data"] == 2


def test_action_metric_lookups():
    t = totals(spend=100, actions={"link_click": 40},
               cost_per_action_type=None)
    assert action_metric(t, "actions", "link_click") == 40
    assert action_metric(t, "actions", "nope") == 0
    assert action_metric(t, "cost_per_action", "link_click") == pytest.approx(2.5)
    assert action_metric(t, "action_values", "purchase") == 0
    t.reported_cost_per_action = {"link_click": 2.4}
    assert action_metric(t, "cost_per_action", "link_click") == 2.4


def test_link_clicks_fall_back_to_actions():
    m = compute_metrics(totals(impressions=1000, actions={"link_click": 15}), PURCHASES)
    assert m["link_clicks"] == 15
    assert m["link_ctr"] == pytest.approx(1.5)


def test_rows_without_id_are_ignored():
    assert group_rows([{"spend": "5"}], "adset") == {}


def test_known_metric_names():
    assert is_known_metric("spend")
    assert is_known_metric("CPA")
    assert is_known_metric("actions.purchase")
    assert is_known_metric("daily_budget")
    assert not is_known_metric("actions")
    assert not is_known_metric("spent")


def test_invalid_numbers_are_zero():
    t = InsightTotals("1")
    t.add_row({"spend": "n/a", "impressions": None, "actions": "bad"})
    assert t.spend == 0 and t.impressions == 0 and t.actions == {}
