import json
from datetime import date

import pytest

from meta_ads_guard.errors import ReportJobError
from meta_ads_guard.marketing import AdObject, MarketingAPI
from meta_ads_guard.timeutil import resolve_window

from .conftest import ACCOUNT, adset, insights_row, respond

TODAY = date(2026, 11, 12)


def test_list_objects_fields_and_status_filter(api, transport):
    transport.add(
        "GET", rf"/{ACCOUNT}/adsets$",
        respond({"data": [adset("1", "A", labels=["winner"])],
                 "paging": {"cursors": {"after": "x"}, "next": "n"}}),
        respond({"data": [adset("2", "B", daily_budget=None, lifetime_budget="900000")]}),
    )
    objs = api.list_objects("adset", ["ACTIVE"])
    assert [o.id for o in objs] == ["1", "2"]
    first = transport.calls[0].params
    assert "daily_budget" in first["fields"] and "adlabels" in first["fields"]
    assert first["effective_status"] == ["ACTIVE"]
    assert objs[0].labels == ["winner"]
    assert objs[0].budget_field == "daily_budget"
    assert objs[1].budget_field == "lifetime_budget"
    assert objs[1].lifetime_budget == 900000
    assert objs[0].created_time.year == 2026


def test_ad_objects_have_parent_ids():
    obj = AdObject.from_api("ad", {"id": "9", "campaign_id": "c", "adset_id": "s",
                                   "adlabels": {"data": [{"name": "x"}]}})
    assert (obj.campaign_id, obj.adset_id, obj.labels) == ("c", "s", ["x"])
    assert obj.budget_field is None


def test_unknown_level(api):
    with pytest.raises(ValueError):
        api.list_objects("account")


def test_sync_insights_params(api, transport):
    transport.add("GET", rf"/{ACCOUNT}/insights$",
                  respond({"data": [insights_row("adset", "1", spend=10)]}))
    rows = api.insights("adset", resolve_window("3d", TODAY))
    assert rows[0]["adset_id"] == "1"
    params = transport.calls[0].params
    assert params["level"] == "adset"
    assert params["time_range"] == {"since": "2026-11-10", "until": "2026-11-12"}
    for f in ("spend", "impressions", "clicks", "ctr", "cpc", "cpm", "frequency", "actions",
              "cost_per_action_type"):
        assert f in params["fields"].split(",")
    assert "action_attribution_windows" not in params


def test_attribution_windows_param(client, transport):
    api = MarketingAPI(client, ACCOUNT, attribution_windows=["7d_click", "1d_view"])
    transport.add("GET", r"/insights$", respond({"data": []}))
    api.insights("ad", resolve_window("today", TODAY))
    assert transport.calls[0].params["action_attribution_windows"] == ["7d_click", "1d_view"]


def test_async_report_flow(client, transport, sleeps):
    api = MarketingAPI(client, ACCOUNT, insights_mode="async", sleep=sleeps.append,
                       poll_interval=2)
    transport.add("POST", rf"/{ACCOUNT}/insights$", respond({"report_run_id": "6020"}))
    transport.add(
        "GET", r"/v24.0/6020$",
        respond({"id": "6020", "async_status": "Job Not Started", "async_percent_completion": 0}),
        respond({"id": "6020", "async_status": "Job Running", "async_percent_completion": 50}),
        respond({"id": "6020", "async_status": "Job Completed", "async_percent_completion": 100}),
    )
    transport.add(
        "GET", r"/v24.0/6020/insights$",
        respond({"data": [insights_row("ad", "1")], "paging": {"cursors": {"after": "a"},
                                                               "next": "n"}}),
        respond({"data": [insights_row("ad", "2")]}),
    )
    rows = api.insights("ad", resolve_window("last_7d", TODAY))
    assert [r["ad_id"] for r in rows] == ["1", "2"]
    post = transport.find("POST", r"/insights$")[0]
    assert post.data["level"] == "ad"
    assert sleeps == [2, 2]


def test_auto_mode_switches_to_async_for_long_ranges(client, transport):
    api = MarketingAPI(client, ACCOUNT, async_threshold_days=30)
    assert not api.use_async(resolve_window("30d", TODAY))
    assert api.use_async(resolve_window("31d", TODAY))
    assert api.use_async(resolve_window("lifetime", TODAY))
    assert not MarketingAPI(client, ACCOUNT, insights_mode="sync").use_async(
        resolve_window("lifetime", TODAY))


@pytest.mark.parametrize("status", ["Job Failed", "Job Skipped"])
def test_async_report_failure(client, transport, sleeps, status):
    api = MarketingAPI(client, ACCOUNT, insights_mode="async", sleep=sleeps.append)
    transport.add("POST", r"/insights$", respond({"report_run_id": "77"}))
    transport.add("GET", r"/77$", respond({"async_status": status,
                                           "error_user_msg": "Please reduce the data"}))
    with pytest.raises(ReportJobError) as info:
        api.insights("ad", resolve_window("3d", TODAY))
    assert status in str(info.value)


def test_async_report_timeout(client, transport):
    clock = iter(range(0, 10_000, 100))
    api = MarketingAPI(client, ACCOUNT, insights_mode="async", sleep=lambda s: None,
                       poll_timeout=250, monotonic=lambda: next(clock))
    transport.add("POST", r"/insights$", respond({"report_run_id": "77"}))
    transport.add("GET", r"/77$", respond({"async_status": "Job Running"}))
    with pytest.raises(ReportJobError, match="still"):
        api.insights("ad", resolve_window("3d", TODAY))


def test_async_report_without_id(client, transport):
    api = MarketingAPI(client, ACCOUNT, insights_mode="async")
    transport.add("POST", r"/insights$", respond({}))
    with pytest.raises(ReportJobError):
        api.insights("ad", resolve_window("3d", TODAY))


def test_invalid_mode(client):
    with pytest.raises(ValueError):
        MarketingAPI(client, ACCOUNT, insights_mode="fast")


def test_mutations(api, transport):
    transport.add("POST", r"/v24.0/111$", respond({"success": True}))
    transport.add("POST", r"/v24.0/222/adlabels$", respond({"success": True}))
    api.pause("111")
    api.set_budget("111", "daily_budget", 165000)
    api.add_label("adset", "111", "fatigued")
    api.add_label("ad", "222", "fatigued")
    pause, budget, adset_label = transport.find("POST", r"/111$")
    assert pause.data["status"] == "PAUSED"
    assert budget.data["daily_budget"] == 165000
    assert adset_label.data["adlabels"] == [{"name": "fatigued"}]
    ad_label = transport.find("POST", r"/222/adlabels$")[0]
    assert ad_label.data["adlabels"] == [{"name": "fatigued"}]
    with pytest.raises(ValueError):
        api.set_budget("1", "bid_amount", 1)


def test_account_info(api, transport, account_info):
    transport.add("GET", rf"/{ACCOUNT}$", account_info)
    info = api.account_info()
    assert info["currency"] == "BDT"
    assert "timezone_name" in transport.calls[0].params["fields"]


def test_json_round_trip_of_params(api):
    params = api.insights_params("campaign", resolve_window("lifetime", TODAY))
    assert params["date_preset"] == "maximum"
    json.dumps(params)
