import json

import pytest

from meta_ads_guard.errors import AuthError, GraphAPIError, RateLimitError, TransportError
from meta_ads_guard.graph import GraphClient, appsecret_proof, normalize_account_id
from meta_ads_guard.http import encode_params, redact_url
from meta_ads_guard.ratelimit import RateLimiter

from .conftest import TOKEN, FakeTransport, graph_error, respond


def test_versioned_url_and_token(client, transport):
    transport.add("GET", r"/v24.0/me$", respond({"id": "1"}))
    assert client.get("me", fields=["id", "name"]) == {"id": "1"}
    call = transport.calls[0]
    assert call.url == "https://graph.facebook.com/v24.0/me"
    assert call.params["access_token"] == TOKEN
    assert call.params["fields"] == "id,name"


def test_post_sends_form_data(client, transport):
    transport.add("POST", r"/v24.0/123$", respond({"success": True}))
    client.post("123", status="PAUSED")
    call = transport.calls[0]
    assert call.data == {"status": "PAUSED", "access_token": TOKEN}
    assert call.params == {}


def test_appsecret_proof_is_added(transport):
    c = GraphClient(TOKEN, app_secret="s3cret", transport=transport,
                    rate_limiter=RateLimiter(sleep=lambda s: None))
    transport.add("GET", r"/me$", respond({}))
    c.get("me")
    assert transport.calls[0].params["appsecret_proof"] == appsecret_proof(TOKEN, "s3cret")
    assert len(appsecret_proof(TOKEN, "s3cret")) == 64


def test_rejects_bad_version_and_token():
    with pytest.raises(ValueError):
        GraphClient(TOKEN, api_version="24")
    with pytest.raises(ValueError):
        GraphClient("")


def test_pagination_follows_after_cursor(client, transport):
    transport.add(
        "GET", r"/act_1/ads$",
        respond({"data": [{"id": "1"}, {"id": "2"}],
                 "paging": {"cursors": {"after": "CUR1"}, "next": "https://x/next"}}),
        respond({"data": [{"id": "3"}], "paging": {"cursors": {"after": "CUR2"}}}),
    )
    items = list(client.paginate("act_1/ads", {"limit": 2}))
    assert [i["id"] for i in items] == ["1", "2", "3"]
    assert "after" not in transport.calls[0].params
    assert transport.calls[1].params["after"] == "CUR1"


def test_pagination_stops_at_max_pages(client, transport):
    transport.add("GET", r"/loop$", respond({"data": [{"id": "x"}],
                                              "paging": {"cursors": {"after": "A"}, "next": "n"}}))
    assert len(list(client.paginate("loop", max_pages=3))) == 3


def test_throttle_error_is_retried_with_regain_time(client, transport, sleeps):
    headers = {"X-Business-Use-Case-Usage": json.dumps(
        {"1": [{"type": "ads_insights", "call_count": 100, "estimated_time_to_regain_access": 2}]}
    )}
    transport.add("GET", r"/me$", graph_error(17, "User request limit reached", 2446079,
                                              headers=headers), respond({"ok": 1}))
    assert client.get("me") == {"ok": 1}
    assert sleeps == [120]
    assert client.calls == 2


def test_throttle_613_backoff_without_hint(client, transport, sleeps):
    transport.add("GET", r"/me$", graph_error(613, "Calls limit", 1487742),
                  graph_error(613, "Calls limit", 1487742), respond({"ok": 1}))
    assert client.get("me") == {"ok": 1}
    assert sleeps == [5, 10]


def test_throttle_gives_up_after_max_retries(client, transport):
    transport.add("GET", r"/me$", graph_error(80004, "BUC"))
    with pytest.raises(RateLimitError) as info:
        client.get("me")
    assert info.value.code == 80004
    assert client.calls == 4  # first try + 3 retries


def test_non_retryable_throttle_raises_immediately(client, transport, sleeps):
    transport.add("POST", r"/42$", graph_error(613, "budget changed too often", 1487632))
    with pytest.raises(RateLimitError):
        client.post("42", daily_budget=100)
    assert client.calls == 1 and sleeps == []


def test_auth_errors(client, transport):
    transport.add("GET", r"/me$", graph_error(190, "Invalid OAuth access token."))
    with pytest.raises(AuthError) as info:
        client.get("me")
    assert info.value.code == 190
    assert "fbtrace_id=AbCdEf" in str(info.value)


def test_permission_error_is_auth_error(client, transport):
    transport.add("GET", r"/me$", graph_error(200, "Permissions error", status=403))
    with pytest.raises(AuthError):
        client.get("me")


def test_other_errors_not_retried(client, transport):
    transport.add("GET", r"/me$", graph_error(100, "Invalid parameter", 1487534))
    with pytest.raises(GraphAPIError) as info:
        client.get("me")
    assert not isinstance(info.value, RateLimitError)
    assert info.value.subcode == 1487534
    assert client.calls == 1


def test_transient_and_5xx_are_retried(client, transport, sleeps):
    transport.add("GET", r"/me$", graph_error(2, "Service temporarily unavailable", status=500),
                  respond(None, status=502), respond({"ok": True}))
    assert client.get("me") == {"ok": True}
    assert len(sleeps) == 2


def test_network_errors_are_retried(client, sleeps):
    class Flaky(FakeTransport):
        n = 0

        def request(self, *a, **kw):
            self.n += 1
            if self.n < 3:
                raise TransportError("timeout")
            return respond({"ok": 1})

    client.transport = Flaky()
    assert client.get("me") == {"ok": 1}
    client.transport = Flaky()
    client.max_retries = 1
    with pytest.raises(TransportError):
        client.get("me")


def test_usage_header_pause_after_success(client, transport, sleeps):
    headers = {"X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 95})}
    transport.add("GET", r"/me$", respond({"ok": 1}, headers=headers))
    client.get("me")
    assert len(sleeps) == 1 and sleeps[0] > 10


def test_non_json_error_body(client, transport):
    transport.add("GET", r"/me$", respond(None, status=404))
    with pytest.raises(GraphAPIError):
        client.get("me")


def test_list_body_is_wrapped(client, transport):
    transport.add("GET", r"/me$", respond([1, 2]))
    assert client.get("me") == {"data": [1, 2]}


@pytest.mark.parametrize("raw, expected", [("123", "act_123"), ("act_123", "act_123"),
                                           (" act_9 ", "act_9"), (456, "act_456")])
def test_normalize_account_id(raw, expected):
    assert normalize_account_id(raw) == expected


def test_normalize_account_id_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_account_id("act_12x")


def test_encode_params_json_values():
    encoded = encode_params({"time_range": {"since": "2026-01-01", "until": "2026-01-02"},
                             "effective_status": ["ACTIVE"], "flag": True, "skip": None})
    assert "time_range=%7B%22since%22" in encoded
    assert "effective_status=%5B%22ACTIVE%22%5D" in encoded
    assert "flag=true" in encoded and "skip" not in encoded


def test_redact_url_hides_tokens():
    url = "https://graph.facebook.com/v24.0/me?access_token=SECRET&fields=id"
    assert "SECRET" not in redact_url(url)
    tg = "https://api.telegram.org/bot123456:ABC-DEF/sendMessage"
    assert "ABC-DEF" not in redact_url(tg)
    assert redact_url("https://example.com/path") == "https://example.com/path"
