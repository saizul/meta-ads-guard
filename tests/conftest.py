"""Shared fixtures: a mocked HTTP layer and a small fake ad account."""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

import pytest

from meta_ads_guard.graph import GraphClient
from meta_ads_guard.http import HttpResponse
from meta_ads_guard.marketing import MarketingAPI
from meta_ads_guard.ratelimit import RateLimiter

TOKEN = "test-token-not-real"
ACCOUNT = "act_1234567890"


@dataclass
class Call:
    method: str
    url: str
    params: dict[str, Any]
    data: Optional[dict[str, Any]]
    json_body: Any

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path

    @property
    def args(self) -> dict[str, Any]:
        """Params or form data, whichever was sent."""
        return dict(self.params or self.data or {})


Responder = Union[HttpResponse, Callable[[Call], HttpResponse]]


def respond(body: Any = None, status: int = 200, headers: Optional[dict[str, str]] = None):
    return HttpResponse(status, headers or {}, json.dumps(body) if body is not None else "")


def graph_error(code: int, message: str = "error", subcode: Optional[int] = None,
                status: int = 400, headers: Optional[dict[str, str]] = None) -> HttpResponse:
    err: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code,
                           "fbtrace_id": "AbCdEf"}
    if subcode is not None:
        err["error_subcode"] = subcode
    return respond({"error": err}, status, headers)


@dataclass
class FakeTransport:
    """Routes requests by method and a regex on the URL path."""

    routes: list[tuple[str, re.Pattern[str], list[Responder]]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def add(self, method: str, pattern: str, *responses: Responder) -> FakeTransport:
        self.routes.append((method.upper(), re.compile(pattern), list(responses)))
        return self

    def request(self, method, url, *, params=None, data=None, json_body=None, headers=None,
                timeout=30.0):
        call = Call(method.upper(), url, dict(params or {}), data and dict(data), json_body)
        self.calls.append(call)
        for m, pattern, responses in self.routes:
            if m == call.method and pattern.search(call.path):
                if not responses:
                    raise AssertionError(f"route {pattern.pattern} exhausted")
                responder = responses[0] if len(responses) == 1 else responses.pop(0)
                return responder(call) if callable(responder) else responder
        raise AssertionError(f"unexpected request {call.method} {call.path} {call.args}")

    def find(self, method: str, pattern: str) -> list[Call]:
        rx = re.compile(pattern)
        return [c for c in self.calls if c.method == method.upper() and rx.search(c.path)]


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(transport: FakeTransport, sleeps: list[float]) -> GraphClient:
    return GraphClient(
        TOKEN,
        api_version="v24.0",
        transport=transport,
        rate_limiter=RateLimiter(sleep=sleeps.append, jitter=lambda: 0.0),
        max_retries=3,
    )


@pytest.fixture
def api(client: GraphClient, sleeps: list[float]) -> MarketingAPI:
    return MarketingAPI(client, ACCOUNT, sleep=sleeps.append, poll_interval=1)


def insights_row(level: str, oid: str, name: str = "", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        f"{level}_id": oid,
        f"{level}_name": name or f"{level} {oid}",
        "account_currency": "BDT",
        "spend": "0",
        "impressions": "0",
        "reach": "0",
        "clicks": "0",
        "date_start": "2026-11-10",
        "date_stop": "2026-11-12",
    }
    actions = kw.pop("actions", None)
    values = kw.pop("action_values", None)
    row.update({k: str(v) for k, v in kw.items()})
    if actions:
        row["actions"] = [{"action_type": k, "value": str(v)} for k, v in actions.items()]
    if values:
        row["action_values"] = [{"action_type": k, "value": str(v)} for k, v in values.items()]
    return row


def adset(oid: str, name: str, *, status: str = "ACTIVE", daily_budget: Optional[int] = 100000,
          labels: Optional[list[str]] = None, **extra: Any) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": oid,
        "name": name,
        "status": status,
        "effective_status": status,
        "campaign_id": "c1",
        "created_time": "2026-11-01T10:00:00+0600",
        "updated_time": "2026-11-05T10:00:00+0600",
    }
    if daily_budget is not None:
        obj["daily_budget"] = str(daily_budget)
    if labels:
        obj["adlabels"] = [{"id": f"l{i}", "name": n} for i, n in enumerate(labels)]
    obj.update(extra)
    return obj


@pytest.fixture
def account_info() -> HttpResponse:
    return respond(
        {"id": ACCOUNT, "name": "Demo Shop BD", "currency": "BDT", "timezone_name": "Asia/Dhaka",
         "account_status": 1}
    )
