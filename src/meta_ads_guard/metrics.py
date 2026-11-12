"""Turn raw insights rows into the metric values rules are written against.

Insights return additive counters (``spend``, ``impressions``, ``clicks``,
``actions``) and ratios (``ctr``, ``cpc``, ``cpm``, ``frequency``). When one
object has several rows for a window (``time_increment`` or a merged
report), counters are summed and ratios are recomputed from the sums, so the
numbers stay consistent.

Conversions are read from ``actions`` / ``action_values``. Meta reports the
same purchase under several action types (``omni_purchase``, ``purchase``,
``offsite_conversion.fb_pixel_purchase``), so each result alias lists its
types in priority order and the **first one present** is used. Adding them up
would count the same conversion two or three times.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

# Alias -> action types, in priority order.
RESULT_ACTION_TYPES: dict[str, tuple[str, ...]] = {
    "purchases": (
        "omni_purchase",
        "purchase",
        "offsite_conversion.fb_pixel_purchase",
        "onsite_web_purchase",
        "onsite_conversion.purchase",
    ),
    "leads": (
        "lead",
        "onsite_conversion.lead_grouped",
        "offsite_conversion.fb_pixel_lead",
        "leadgen_grouped",
    ),
    "add_to_cart": (
        "omni_add_to_cart",
        "add_to_cart",
        "offsite_conversion.fb_pixel_add_to_cart",
    ),
    "initiate_checkout": (
        "omni_initiated_checkout",
        "initiate_checkout",
        "offsite_conversion.fb_pixel_initiate_checkout",
    ),
    "complete_registration": (
        "omni_complete_registration",
        "complete_registration",
        "offsite_conversion.fb_pixel_complete_registration",
    ),
    "messages": (
        "onsite_conversion.messaging_conversation_started_7d",
        "onsite_conversion.total_messaging_connection",
    ),
    "landing_page_views": ("omni_landing_page_view", "landing_page_view"),
    "video_views": ("video_view",),
    "post_engagement": ("post_engagement",),
}

# Insights fields requested for every level.
INSIGHT_FIELDS: tuple[str, ...] = (
    "account_currency",
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "spend",
    "impressions",
    "reach",
    "clicks",
    "inline_link_clicks",
    "ctr",
    "cpc",
    "cpm",
    "frequency",
    "actions",
    "action_values",
    "cost_per_action_type",
    "purchase_roas",
    "date_start",
    "date_stop",
)

# Metric names available in every window, with a short description.
WINDOW_METRICS: dict[str, str] = {
    "spend": "amount spent, account currency",
    "impressions": "impressions",
    "reach": "unique accounts reached",
    "clicks": "all clicks",
    "link_clicks": "inline link clicks",
    "ctr": "click-through rate, percent (clicks / impressions x 100)",
    "link_ctr": "link click-through rate, percent",
    "cpc": "cost per click",
    "cpm": "cost per 1,000 impressions",
    "frequency": "impressions / reach",
    "results": "conversions of the rule's result type",
    "cost_per_result": "spend / results (missing when results is 0)",
    "cpa": "alias of cost_per_result",
    "result_value": "conversion value of the rule's result type",
    "roas": "result_value / spend",
    "purchase_roas": "Meta reported purchase ROAS",
    "conversion_rate": "results / link_clicks x 100",
    **{alias: f"{alias.replace('_', ' ')} (first matching action type)" for alias in
       RESULT_ACTION_TYPES},
    "purchase_value": "purchase conversion value",
    "cost_per_purchase": "spend / purchases",
    "cost_per_lead": "spend / leads",
    "cost_per_message": "spend / messages",
    "days_with_data": "number of insights rows merged (1 for an aggregated range)",
}

# Prefixes for raw action lookups, e.g. actions.link_click.
ACTION_PREFIXES = ("actions", "action_values", "cost_per_action")

# Object fields (not tied to a window).
OBJECT_METRICS: dict[str, str] = {
    "daily_budget": "daily budget in major currency units (missing without one)",
    "lifetime_budget": "lifetime budget in major currency units",
    "budget": "daily budget, or lifetime budget when there is no daily budget",
    "age_hours": "hours since the object was created",
    "age_days": "days since the object was created",
    "hours_since_update": "hours since the object was last updated",
}


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _action_map(items: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict) or "action_type" not in item:
            continue
        key = str(item["action_type"])
        out[key] = out.get(key, 0.0) + _f(item.get("value"))
    return out


@dataclass
class InsightTotals:
    """Summed counters for one object over one window."""

    object_id: str
    name: str = ""
    currency: Optional[str] = None
    spend: float = 0.0
    impressions: float = 0.0
    reach: float = 0.0
    clicks: float = 0.0
    link_clicks: float = 0.0
    actions: dict[str, float] = field(default_factory=dict)
    action_values: dict[str, float] = field(default_factory=dict)
    reported_cost_per_action: dict[str, float] = field(default_factory=dict)
    reported_frequency: Optional[float] = None
    reported_purchase_roas: Optional[float] = None
    rows: int = 0
    date_start: Optional[str] = None
    date_stop: Optional[str] = None

    def add_row(self, row: dict[str, Any]) -> None:
        self.rows += 1
        self.spend += _f(row.get("spend"))
        self.impressions += _f(row.get("impressions"))
        self.reach += _f(row.get("reach"))
        self.clicks += _f(row.get("clicks"))
        self.link_clicks += _f(row.get("inline_link_clicks"))
        for k, v in _action_map(row.get("actions")).items():
            self.actions[k] = self.actions.get(k, 0.0) + v
        for k, v in _action_map(row.get("action_values")).items():
            self.action_values[k] = self.action_values.get(k, 0.0) + v
        if row.get("account_currency"):
            self.currency = str(row["account_currency"])
        if self.rows == 1:
            self.reported_cost_per_action = _action_map(row.get("cost_per_action_type"))
            if row.get("frequency") not in (None, ""):
                self.reported_frequency = _f(row.get("frequency"))
            roas = _action_map(row.get("purchase_roas"))
            if roas:
                self.reported_purchase_roas = next(iter(roas.values()))
        else:
            # Ratios cannot be summed; recompute from counters instead.
            self.reported_cost_per_action = {}
            self.reported_frequency = None
            self.reported_purchase_roas = None
        start, stop = row.get("date_start"), row.get("date_stop")
        if start and (self.date_start is None or start < self.date_start):
            self.date_start = start
        if stop and (self.date_stop is None or stop > self.date_stop):
            self.date_stop = stop


def first_present(values: dict[str, float], types: Iterable[str]) -> Optional[float]:
    for t in types:
        if t in values:
            return values[t]
    return None


def _div(a: Optional[float], b: Optional[float], scale: float = 1.0) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b * scale


def resolve_result_types(result: Any) -> tuple[str, ...]:
    """``purchases`` -> its action types; a list or ``action:<type>`` -> itself."""
    if isinstance(result, (list, tuple)):
        return tuple(str(r) for r in result)
    text = str(result)
    if text in RESULT_ACTION_TYPES:
        return RESULT_ACTION_TYPES[text]
    if text.startswith(("action:", "actions.")):
        return (text.split(":", 1)[1] if ":" in text else text.split(".", 1)[1],)
    return (text,)


def compute_metrics(
    totals: Optional[InsightTotals], result_types: tuple[str, ...]
) -> dict[str, Optional[float]]:
    """Return every window metric for ``totals``.

    An object with no insights row for the window (it did not deliver) gets
    zero counters, so ``spend == 0`` and ``purchases == 0`` hold for it.
    """
    t = totals or InsightTotals(object_id="")
    m: dict[str, Optional[float]] = {
        "spend": round(t.spend, 6),
        "impressions": t.impressions,
        "reach": t.reach,
        "clicks": t.clicks,
        "link_clicks": t.link_clicks or t.actions.get("link_click", 0.0),
        "days_with_data": float(t.rows),
    }
    m["ctr"] = _div(t.clicks, t.impressions, 100.0)
    m["link_ctr"] = _div(m["link_clicks"], t.impressions, 100.0)
    m["cpc"] = _div(t.spend, t.clicks)
    m["cpm"] = _div(t.spend, t.impressions, 1000.0)
    if t.reported_frequency is not None:
        m["frequency"] = t.reported_frequency
    else:
        m["frequency"] = _div(t.impressions, t.reach)

    for alias, types in RESULT_ACTION_TYPES.items():
        m[alias] = first_present(t.actions, types) or 0.0
    m["purchase_value"] = first_present(t.action_values, RESULT_ACTION_TYPES["purchases"]) or 0.0
    m["cost_per_purchase"] = _div(t.spend, m["purchases"])
    m["cost_per_lead"] = _div(t.spend, m["leads"])
    m["cost_per_message"] = _div(t.spend, m["messages"])

    results = first_present(t.actions, result_types) or 0.0
    m["results"] = results
    m["cost_per_result"] = _div(t.spend, results)
    m["cpa"] = m["cost_per_result"]
    m["result_value"] = first_present(t.action_values, result_types) or 0.0
    m["roas"] = _div(m["result_value"], t.spend)
    m["purchase_roas"] = (
        t.reported_purchase_roas
        if t.reported_purchase_roas is not None
        else _div(m["purchase_value"], t.spend)
    )
    m["conversion_rate"] = _div(results, m["link_clicks"], 100.0)
    return m


def action_metric(
    totals: Optional[InsightTotals], prefix: str, action_type: str
) -> Optional[float]:
    t = totals or InsightTotals(object_id="")
    if prefix == "actions":
        return t.actions.get(action_type, 0.0)
    if prefix == "action_values":
        return t.action_values.get(action_type, 0.0)
    if prefix == "cost_per_action":
        if action_type in t.reported_cost_per_action:
            return t.reported_cost_per_action[action_type]
        return _div(t.spend, t.actions.get(action_type))
    raise KeyError(prefix)


def group_rows(rows: Iterable[dict[str, Any]], level: str) -> dict[str, InsightTotals]:
    """Group insights rows by ``<level>_id``."""
    key = f"{level}_id"
    name_key = f"{level}_name" if level != "account" else "account_name"
    out: dict[str, InsightTotals] = {}
    for row in rows:
        oid = str(row.get(key) or row.get("account_id") or "")
        if not oid:
            continue
        totals = out.get(oid)
        if totals is None:
            totals = out[oid] = InsightTotals(object_id=oid, name=str(row.get(name_key) or ""))
        totals.add_row(row)
    return out


def is_known_metric(name: str) -> bool:
    lowered = name.lower()
    if lowered in WINDOW_METRICS or lowered in OBJECT_METRICS:
        return True
    prefix, _, rest = lowered.partition(".")
    return prefix in ACTION_PREFIXES and bool(rest)
