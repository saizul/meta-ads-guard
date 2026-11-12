import json

import pytest

from meta_ads_guard.ratelimit import (
    RateLimiter,
    is_retryable_throttle,
    is_throttle,
    parse_usage,
)


def buc(**entry):
    return {"X-Business-Use-Case-Usage": json.dumps({"66782684": [{"type": "ads_management",
                                                                    **entry}]})}


def test_parse_business_use_case_header():
    snap = parse_usage(buc(call_count=95, total_cputime=20, total_time=30,
                           estimated_time_to_regain_access=0,
                           ads_api_access_tier="development_access"))
    assert snap.max_pct == 95
    assert snap.regain_seconds == 0
    assert snap.tier == "development_access"
    assert "buc:66782684:ads_management" in snap.sources


def test_regain_minutes_become_seconds():
    snap = parse_usage(buc(call_count=100, estimated_time_to_regain_access=3))
    assert snap.regain_seconds == 180


def test_header_names_are_case_insensitive_and_combined():
    headers = {
        "x-ad-account-usage": json.dumps({"acc_id_util_pct": 40, "reset_time_duration": 50}),
        "X-FB-Ads-Insights-Throttle": json.dumps({"app_id_util_pct": 12, "acc_id_util_pct": 88}),
        "x-app-usage": json.dumps({"call_count": 5, "total_time": 7, "total_cputime": 1}),
    }
    snap = parse_usage(headers)
    assert snap.max_pct == 88
    assert snap.sources["ad_account"] == 40
    assert snap.regain_seconds == 0  # reset only counts once the account is at 100 %


def test_ad_account_reset_counts_at_full_usage():
    snap = parse_usage({"X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 100,
                                                          "reset_time_duration": 42})})
    assert snap.regain_seconds == 42


def test_garbage_headers_are_ignored():
    snap = parse_usage({"X-Business-Use-Case-Usage": "{not json", "X-App-Usage": "[]"})
    assert snap.empty


def test_limiter_pauses_above_threshold():
    slept = []
    rl = RateLimiter(threshold=80, base_pause=10, sleep=slept.append)
    assert rl.after_response(buc(call_count=50)) == 0
    assert rl.after_response(buc(call_count=80)) == pytest.approx(10)
    assert rl.after_response(buc(call_count=100)) == pytest.approx(60)
    assert slept == [pytest.approx(10), pytest.approx(60)]
    assert rl.total_slept == pytest.approx(70)


def test_limiter_honours_regain_time_with_cap():
    slept = []
    rl = RateLimiter(max_wait=120, sleep=slept.append)
    rl.after_response(buc(call_count=10, estimated_time_to_regain_access=5))
    assert slept == [120]


def test_no_headers_no_pause():
    slept = []
    rl = RateLimiter(sleep=slept.append)
    assert rl.after_response({"content-type": "application/json"}) == 0
    assert slept == []


def test_retry_delay_backoff():
    rl = RateLimiter(backoff_base=2, max_wait=30, jitter=lambda: 0.0)
    assert [rl.retry_delay(i) for i in range(5)] == [2, 4, 8, 16, 30]
    assert rl.retry_delay(0, retry_after=9) == 9
    jittery = RateLimiter(backoff_base=4, jitter=lambda: 1.0)
    assert jittery.retry_delay(0) == 5


def test_invalid_threshold():
    with pytest.raises(ValueError):
        RateLimiter(threshold=0)


@pytest.mark.parametrize("code", [4, 17, 32, 613, 80000, 80004, 80014])
def test_throttle_codes(code):
    assert is_throttle(code)


def test_non_throttle_and_non_retryable():
    assert not is_throttle(100)
    assert not is_throttle(None)
    assert is_retryable_throttle(17, 2446079)
    assert not is_retryable_throttle(613, 1487632)  # budget changed > 4 times an hour
    assert not is_retryable_throttle(17, 1885172)
