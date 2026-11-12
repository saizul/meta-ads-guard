"""Marketing API calls: ad objects, insights (sync and async) and mutations."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from .errors import ReportJobError
from .graph import GraphClient, normalize_account_id
from .metrics import INSIGHT_FIELDS
from .timeutil import Window, parse_graph_time

log = logging.getLogger(__name__)

LEVELS = ("campaign", "adset", "ad")
EDGES = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}

_COMMON_FIELDS = (
    "id",
    "name",
    "status",
    "effective_status",
    "created_time",
    "updated_time",
    "adlabels",
)
OBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "campaign": (*_COMMON_FIELDS, "objective", "daily_budget", "lifetime_budget"),
    "adset": (*_COMMON_FIELDS, "campaign_id", "daily_budget", "lifetime_budget"),
    "ad": (*_COMMON_FIELDS, "campaign_id", "adset_id"),
}

ASYNC_DONE = "Job Completed"
ASYNC_FAILED = frozenset({"Job Failed", "Job Skipped"})


@dataclass
class AdObject:
    id: str
    level: str
    name: str = ""
    status: str = ""
    effective_status: str = ""
    daily_budget: Optional[int] = None
    lifetime_budget: Optional[int] = None
    campaign_id: Optional[str] = None
    adset_id: Optional[str] = None
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    labels: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, level: str, data: dict[str, Any]) -> AdObject:
        labels_raw = data.get("adlabels")
        if isinstance(labels_raw, dict):
            labels_raw = labels_raw.get("data")
        labels = [str(lbl.get("name")) for lbl in labels_raw or [] if isinstance(lbl, dict)]
        return cls(
            id=str(data["id"]),
            level=level,
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            effective_status=str(data.get("effective_status") or ""),
            daily_budget=_budget(data.get("daily_budget")),
            lifetime_budget=_budget(data.get("lifetime_budget")),
            campaign_id=_opt(data.get("campaign_id")),
            adset_id=_opt(data.get("adset_id")),
            created_time=parse_graph_time(data.get("created_time")),
            updated_time=parse_graph_time(data.get("updated_time")),
            labels=labels,
            raw=data,
        )

    @property
    def budget_field(self) -> Optional[str]:
        if self.daily_budget:
            return "daily_budget"
        if self.lifetime_budget:
            return "lifetime_budget"
        return None


def _budget(value: Any) -> Optional[int]:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number or None


def _opt(value: Any) -> Optional[str]:
    return None if value in (None, "") else str(value)


class MarketingAPI:
    """Ad account scoped helper on top of :class:`GraphClient`."""

    def __init__(
        self,
        client: GraphClient,
        account_id: str,
        *,
        insights_mode: str = "auto",
        async_threshold_days: int = 30,
        poll_interval: float = 5.0,
        poll_timeout: float = 1800.0,
        attribution_windows: Optional[list[str]] = None,
        page_size: int = 200,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if insights_mode not in {"auto", "sync", "async"}:
            raise ValueError("insights_mode must be auto, sync or async")
        self.client = client
        self.account_id = normalize_account_id(account_id)
        self.insights_mode = insights_mode
        self.async_threshold_days = async_threshold_days
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.attribution_windows = attribution_windows
        self.page_size = page_size
        self._sleep = sleep
        self._monotonic = monotonic

    # -- reads ---------------------------------------------------------------

    def account_info(self) -> dict[str, Any]:
        return self.client.get(
            self.account_id, fields=["name", "currency", "timezone_name", "account_status"]
        )

    def list_objects(
        self, level: str, effective_status: Optional[list[str]] = None
    ) -> list[AdObject]:
        if level not in EDGES:
            raise ValueError(f"unknown level {level!r}")
        params: dict[str, Any] = {"fields": list(OBJECT_FIELDS[level]), "limit": self.page_size}
        if effective_status:
            params["effective_status"] = list(effective_status)
        path = f"{self.account_id}/{EDGES[level]}"
        return [AdObject.from_api(level, item) for item in self.client.paginate(path, params)]

    def insights_params(self, level: str, window: Window) -> dict[str, Any]:
        params: dict[str, Any] = {
            "level": level,
            "fields": list(INSIGHT_FIELDS),
            "limit": 500,
            **window.insights_params(),
        }
        if self.attribution_windows:
            params["action_attribution_windows"] = list(self.attribution_windows)
        return params

    def use_async(self, window: Window) -> bool:
        if self.insights_mode == "async":
            return True
        if self.insights_mode == "sync":
            return False
        return window.days > self.async_threshold_days

    def insights(self, level: str, window: Window) -> list[dict[str, Any]]:
        """Every insights row for ``level`` in the account over ``window``."""
        params = self.insights_params(level, window)
        if self.use_async(window):
            return self.insights_async(params)
        return list(self.client.paginate(f"{self.account_id}/insights", params))

    def insights_async(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run an Ad Report Run job, poll it, then page through the result."""
        job = self.client.post(f"{self.account_id}/insights", **params)
        run_id = job.get("report_run_id")
        if not run_id:
            raise ReportJobError(f"no report_run_id in response: {job!r}")
        run_id = str(run_id)
        started = self._monotonic()
        status: dict[str, Any] = {}
        while True:
            status = self.client.get(
                run_id,
                fields=[
                    "async_status",
                    "async_percent_completion",
                    "error_code",
                    "error_message",
                    "error_subcode",
                    "error_user_msg",
                ],
            )
            state = status.get("async_status")
            pct = status.get("async_percent_completion")
            log.info("report %s: %s (%s%%)", run_id, state, pct)
            if state == ASYNC_DONE:
                break
            if state in ASYNC_FAILED:
                message = status.get("error_user_msg") or status.get("error_message") or state
                raise ReportJobError(f"report {run_id} {state}: {message}")
            if self._monotonic() - started > self.poll_timeout:
                raise ReportJobError(
                    f"report {run_id} still {state!r} after {self.poll_timeout:.0f}s"
                )
            self._sleep(self.poll_interval)
        return list(self.client.paginate(f"{run_id}/insights", {"limit": 500}))

    # -- writes --------------------------------------------------------------

    def pause(self, object_id: str) -> dict[str, Any]:
        return self.client.post(object_id, status="PAUSED")

    def set_budget(self, object_id: str, budget_field: str, minor_units: int) -> dict[str, Any]:
        if budget_field not in {"daily_budget", "lifetime_budget"}:
            raise ValueError(f"invalid budget field {budget_field!r}")
        return self.client.post(object_id, **{budget_field: int(minor_units)})

    def add_label(self, level: str, object_id: str, label: str) -> dict[str, Any]:
        """Attach a label by name; Meta creates the label if it does not exist.

        Campaigns and ads have an ``/{id}/adlabels`` edge. Ad sets take the
        ``adlabels`` parameter on the object itself.
        """
        labels = [{"name": label}]
        if level == "adset":
            return self.client.post(object_id, adlabels=labels)
        return self.client.post(f"{object_id}/adlabels", adlabels=labels)
