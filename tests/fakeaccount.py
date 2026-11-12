"""A fake ad account served through FakeTransport, used by engine and CLI tests."""

from __future__ import annotations

from typing import Any, Optional

from .conftest import ACCOUNT, FakeTransport, adset, insights_row, respond


class FakeAccount:
    """Holds ad sets/ads and insights per window, answers Graph requests for them."""

    def __init__(self, transport: FakeTransport, currency: str = "BDT"):
        self.transport = transport
        self.currency = currency
        self.objects: dict[str, list[dict[str, Any]]] = {"campaign": [], "adset": [], "ad": []}
        # (level, since or preset) -> rows
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.mutations: list[tuple[str, dict[str, Any]]] = []
        self.fail_mutation: Optional[Any] = None
        transport.add("GET", rf"/{ACCOUNT}$", lambda c: respond(
            {"id": ACCOUNT, "name": "Demo Shop BD", "currency": self.currency,
             "timezone_name": "Asia/Dhaka"}))
        for level, edge in (("campaign", "campaigns"), ("adset", "adsets"), ("ad", "ads")):
            transport.add("GET", rf"/{ACCOUNT}/{edge}$", self._lister(level))
        transport.add("GET", rf"/{ACCOUNT}/insights$", self._insights)
        transport.add("POST", r"/v\d+\.\d+/\d+(/adlabels)?$", self._mutate)

    def add_adset(self, oid: str, name: str, **kw: Any) -> None:
        self.objects["adset"].append(adset(oid, name, **kw))

    def add_ad(self, oid: str, name: str, **kw: Any) -> None:
        obj = adset(oid, name, daily_budget=None, **kw)
        obj["adset_id"] = kw.get("adset_id", "s1")
        self.objects["ad"].append(obj)

    def insights(self, level: str, start: str, oid: str, **metrics: Any) -> None:
        self.rows.setdefault((level, start), []).append(insights_row(level, oid, **metrics))

    def _lister(self, level: str):
        def handler(call):
            statuses = call.params.get("effective_status")
            data = [o for o in self.objects[level]
                    if not statuses or o["effective_status"] in statuses]
            return respond({"data": data})
        return handler

    def _insights(self, call):
        params = call.params
        key = params["time_range"]["since"] if "time_range" in params else params["date_preset"]
        rows = self.rows.get((params["level"], key))
        if rows is None:
            rows = self.rows.get((params["level"], "*"), [])
        return respond({"data": rows})

    def _mutate(self, call):
        if self.fail_mutation is not None:
            return self.fail_mutation
        self.mutations.append((call.path.split("/", 2)[2], dict(call.data)))
        return respond({"success": True})
