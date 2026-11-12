"""Run the engine against a fake, in-memory ad account. No token, no network.

    python examples/offline_demo.py

Shows the dry-run report a real run would print, using the same rule
engine, metrics and budget logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from meta_ads_guard.config import parse_config
from meta_ads_guard.engine import Engine
from meta_ads_guard.graph import GraphClient
from meta_ads_guard.http import HttpResponse
from meta_ads_guard.marketing import MarketingAPI
from meta_ads_guard.report import render_run

ADSETS = [
    {"id": "120200000000000101", "name": "BD | Cold | Video", "status": "ACTIVE",
     "effective_status": "ACTIVE", "daily_budget": "150000",
     "created_time": "2026-11-01T10:00:00+0600"},
    {"id": "120200000000000102", "name": "BD | Retargeting | Carousel", "status": "ACTIVE",
     "effective_status": "ACTIVE", "daily_budget": "200000",
     "created_time": "2026-10-20T10:00:00+0600"},
    {"id": "120200000000000103", "name": "BD | Lookalike 1%", "status": "ACTIVE",
     "effective_status": "ACTIVE", "daily_budget": "300000",
     "created_time": "2026-10-01T10:00:00+0600"},
]
INSIGHTS = [
    {"adset_id": "120200000000000101", "adset_name": "BD | Cold | Video", "spend": "1650.50",
     "impressions": "40000", "reach": "15000", "clicks": "300", "frequency": "2.67"},
    {"adset_id": "120200000000000102", "adset_name": "BD | Retargeting | Carousel",
     "spend": "2200", "impressions": "30000", "reach": "12000", "clicks": "450",
     "actions": [{"action_type": "omni_purchase", "value": "2"}],
     "action_values": [{"action_type": "omni_purchase", "value": "3000"}]},
    {"adset_id": "120200000000000103", "adset_name": "BD | Lookalike 1%", "spend": "5000",
     "impressions": "90000", "reach": "20000", "clicks": "1800",
     "actions": [{"action_type": "omni_purchase", "value": "20"}],
     "action_values": [{"action_type": "omni_purchase", "value": "25000"}]},
]


class OfflineTransport:
    def request(self, method, url, *, params=None, data=None, json_body=None, headers=None,
                timeout=30.0):
        if url.endswith("/act_1234567890"):
            body = {"name": "Demo Shop", "currency": "BDT", "timezone_name": "Asia/Dhaka"}
        elif url.endswith("/adsets"):
            body = {"data": ADSETS}
        elif url.endswith("/insights"):
            body = {"data": INSIGHTS}
        else:
            body = {"success": True}
        return HttpResponse(200, {}, json.dumps(body))


CONFIG = {
    "account": {"id": "act_1234567890", "timezone": "Asia/Dhaka"},
    "vars": {"target_cpa": 450},
    "rules": [
        {"name": "stop-spend-without-purchases",
         "when": "spend > 1500 BDT and purchases == 0 in last 3 days", "actions": ["pause"]},
        {"name": "cpa-above-target", "when": "cpa > target_cpa × 1.4 and results >= 2",
         "actions": [{"scale_budget": {"percent": -20, "min": 500}}]},
        {"name": "scale-winners", "when": "roas > 3 and spend > 2000",
         "actions": [{"scale_budget": {"percent": 15, "max": 20000}}]},
    ],
}


def main() -> None:
    client = GraphClient("offline-demo", transport=OfflineTransport())
    api = MarketingAPI(client, "act_1234567890")
    config = parse_config(CONFIG, env={})
    engine = Engine(config, api, now=lambda: datetime(2026, 11, 12, 6, tzinfo=timezone.utc))
    print(render_run(engine.run()))


if __name__ == "__main__":
    main()
