"""Evaluate rules against an ad account and plan or apply their actions."""

from __future__ import annotations

import logging
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any, Callable, Optional

from .audit import AuditLog
from .config import Config
from .currency import format_money, to_major, to_minor
from .errors import ConfigError, GraphAPIError, MetaAdsGuardError
from .expr import Value, explain
from .marketing import AdObject, MarketingAPI
from .metrics import (
    ACTION_PREFIXES,
    InsightTotals,
    action_metric,
    compute_metrics,
    group_rows,
)
from .notify import Notifier, NotifyError
from .rules import LabelAction, NotifyAction, PauseAction, Rule, ScaleBudgetAction
from .state import MemoryState, StateStore, cooldown_key
from .timeutil import (
    Window,
    canonical_window,
    format_duration,
    get_timezone,
    resolve_window,
    utcnow,
)

log = logging.getLogger(__name__)

PLANNED, APPLIED, SKIPPED, FAILED, QUEUED = "planned", "applied", "skipped", "failed", "queued"


@dataclass
class ActionResult:
    kind: str
    description: str
    status: str
    detail: str = ""
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "status": self.status,
            "detail": self.detail,
            "before": self.before,
            "after": self.after,
        }


@dataclass
class Match:
    rule: Rule
    obj: AdObject
    values: list[tuple[str, Value]]
    metrics: dict[str, Optional[float]]
    status: str = "acted"  # acted | cooldown | capped
    detail: str = ""
    results: list[ActionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.name,
            "level": self.obj.level,
            "object_id": self.obj.id,
            "object_name": self.obj.name,
            "status": self.status,
            "detail": self.detail,
            "values": dict(self.values),
            "actions": [r.to_dict() for r in self.results],
        }


@dataclass
class Notification:
    rule: Rule
    obj: AdObject
    text: str
    channels: Optional[tuple[str, ...]]


@dataclass
class RunReport:
    run_id: str
    started_at: datetime
    dry_run: bool
    account_id: str
    account_name: str = ""
    currency: Optional[str] = None
    timezone: str = "UTC"
    checked: dict[str, int] = field(default_factory=dict)
    matches: list[Match] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notifications_sent: int = 0
    api_calls: int = 0
    finished_at: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.matches:
            for r in m.results:
                out[r.status] = out.get(r.status, 0) + 1
            if m.status != "acted":
                out[m.status] = out.get(m.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "dry_run": self.dry_run,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "currency": self.currency,
            "timezone": self.timezone,
            "checked": self.checked,
            "counts": self.counts(),
            "matches": [m.to_dict() for m in self.matches],
            "errors": self.errors,
            "notifications_sent": self.notifications_sent,
            "api_calls": self.api_calls,
        }


class _Formatter(string.Formatter):
    """``str.format`` that prints missing values as ``n/a`` and numbers nicely."""

    def get_value(self, key: Any, args: Any, kwargs: Any) -> Any:
        if isinstance(key, str):
            return kwargs.get(key)
        return super().get_value(key, args, kwargs)

    def format_field(self, value: Any, format_spec: str) -> str:
        if value is None:
            return "n/a"
        if not format_spec and isinstance(value, float):
            return pretty_number(value)
        return super().format_field(value, format_spec)


def pretty_number(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.2f}"


DEFAULT_MESSAGE = (
    "{rule}: {level} \"{name}\" ({id}) matched over {window}. "
    "Spend {spend} {currency}, results {results}, CPA {cpa}, CTR {ctr}%, "
    "frequency {frequency}. Actions: {actions}"
)


class Engine:
    def __init__(
        self,
        config: Config,
        api: MarketingAPI,
        *,
        state: Optional[StateStore] = None,
        audit: Optional[AuditLog] = None,
        notifiers: Optional[dict[str, Notifier]] = None,
        apply: bool = False,
        notify_in_dry_run: bool = False,
        now: Callable[[], datetime] = utcnow,
    ):
        self.config = config
        self.api = api
        self.state = state if state is not None else MemoryState()
        self.audit = audit if audit is not None else AuditLog(None)
        self.notifiers = notifiers or {}
        self.apply = apply
        self.notify_in_dry_run = notify_in_dry_run
        self._now = now
        self._objects: dict[tuple[str, tuple[str, ...]], list[AdObject]] = {}
        self._registry: dict[str, AdObject] = {}
        self._insights: dict[tuple[str, str], dict[str, InsightTotals]] = {}
        self._actions_taken = 0
        self._tz: tzinfo = get_timezone(config.timezone)
        self._currency: Optional[str] = config.currency
        self._notifications: list[Notification] = []

    # -- data ----------------------------------------------------------------

    def _load_account(self, report: RunReport) -> None:
        info = self.api.account_info()
        report.account_name = str(info.get("name") or "")
        api_currency = info.get("currency")
        if self.config.currency and api_currency and api_currency != self.config.currency:
            raise ConfigError(
                f"account.currency is {self.config.currency} but the ad account reports "
                f"{api_currency}"
            )
        self._currency = self.config.currency or api_currency
        if not self.config.timezone and info.get("timezone_name"):
            self._tz = get_timezone(str(info["timezone_name"]))
        report.currency = self._currency
        report.timezone = str(self.config.timezone or info.get("timezone_name") or "UTC")
        if self._currency:
            for rule in self.config.rules:
                other = rule.condition.currencies() - {self._currency}
                if other:
                    raise ConfigError(
                        f"rule {rule.name!r} uses {sorted(other)} but the account currency "
                        f"is {self._currency}"
                    )

    def objects(self, level: str, statuses: tuple[str, ...]) -> list[AdObject]:
        key = (level, tuple(sorted(statuses)))
        if key not in self._objects:
            fetched = self.api.list_objects(level, list(statuses) or None)
            objs = []
            for obj in fetched:
                # Reuse the instance another rule already changed during this run.
                objs.append(self._registry.setdefault(obj.id, obj))
            self._objects[key] = objs
        return self._objects[key]

    def insights(self, level: str, window: Window) -> dict[str, InsightTotals]:
        key = (level, window.key)
        if key not in self._insights:
            rows = self.api.insights(level, window)
            self._insights[key] = group_rows(rows, level)
        return self._insights[key]

    # -- evaluation ----------------------------------------------------------

    def _resolver(
        self,
        rule: Rule,
        obj: AdObject,
        windows: dict[str, Window],
        cache: dict[str, dict[str, Optional[float]]],
    ) -> Callable[[str, Optional[str]], Value]:
        now = self._now()

        def resolve(name: str, window_spec: Optional[str]) -> Value:
            lowered = name.lower()
            if lowered in rule.vars:
                return rule.vars[lowered]
            obj_value = object_metric(obj, lowered, self._currency, now)
            if obj_value is not _MISSING:
                return obj_value  # type: ignore[return-value]
            key = rule.window if window_spec is None else canonical_window(window_spec)
            window = windows[key]
            totals = self.insights(rule.level, window).get(obj.id)
            prefix, _, action_type = name.partition(".")
            if prefix.lower() in ACTION_PREFIXES and action_type:
                return action_metric(totals, prefix.lower(), action_type)
            if key not in cache:
                cache[key] = compute_metrics(totals, rule.result)
            return cache[key].get(lowered)

        return resolve

    def evaluate_rule(self, rule: Rule, report: RunReport) -> list[Match]:
        today = self._now().astimezone(self._tz).date()
        windows = {key: resolve_window(key, today) for key in rule.windows()}
        objs = [o for o in self.objects(rule.level, rule.filters.effective_status)
                if rule.filters.matches(o)]
        report.checked[rule.name] = len(objs)
        matches = []
        for obj in objs:
            cache: dict[str, dict[str, Optional[float]]] = {}
            resolver = self._resolver(rule, obj, windows, cache)
            if not rule.condition.evaluate(resolver):
                continue
            values = explain(rule.condition, resolver)
            resolver("spend", None)  # make sure the default window metrics are cached
            metrics = dict(cache.get(rule.window, {}))
            matches.append(Match(rule, obj, values, metrics))
        return matches

    # -- actions -------------------------------------------------------------

    def _execute(self, match: Match, report: RunReport, windows_label: str) -> None:
        rule, obj = match.rule, match.obj
        now = self._now()
        key = cooldown_key(self.config.cooldown_scope, rule.name, obj.id)
        last = self.state.last_action(key)
        if last is not None and rule.cooldown > timedelta(0) and now - last < rule.cooldown:
            remaining = rule.cooldown - (now - last)
            match.status = "cooldown"
            match.detail = f"cooldown, {format_duration(remaining)} left"
            return
        if self._actions_taken >= self.config.max_actions_per_run:
            match.status = "capped"
            match.detail = f"safety.max_actions_per_run ({self.config.max_actions_per_run}) reached"
            return
        self._actions_taken += 1

        notify_actions = []
        for action in rule.actions:
            if isinstance(action, NotifyAction):
                notify_actions.append(action)
                continue
            result = self._run_action(action, obj, report)
            match.results.append(result)
            self._audit(match, result)

        for action in notify_actions:
            text = self.render_message(match, action, windows_label)
            self._notifications.append(Notification(rule, obj, text, action.channels))
            sending = self.apply or self.notify_in_dry_run
            result = ActionResult(
                "notify", action.describe(), QUEUED if sending else PLANNED, text
            )
            match.results.append(result)
            self._audit(match, result)

        if self.apply and any(r.status in {APPLIED, QUEUED} for r in match.results):
            self.state.record(key, now, rule.name, obj.id, [r.kind for r in match.results])

    def _run_action(self, action: Any, obj: AdObject, report: RunReport) -> ActionResult:
        try:
            if isinstance(action, PauseAction):
                return self._pause(obj)
            if isinstance(action, ScaleBudgetAction):
                return self._scale(action, obj)
            if isinstance(action, LabelAction):
                return self._label(action, obj)
        except GraphAPIError as exc:
            report.errors.append(f"{action.describe()} on {obj.level} {obj.id}: {exc}")
            return ActionResult(action.kind, action.describe(), FAILED, str(exc))
        raise AssertionError(f"unhandled action {action!r}")

    def _pause(self, obj: AdObject) -> ActionResult:
        if obj.status == "PAUSED":
            return ActionResult("pause", "pause", SKIPPED, "already paused")
        before = obj.status
        if self.apply:
            self.api.pause(obj.id)
        obj.status = "PAUSED"
        obj.effective_status = "PAUSED"
        return ActionResult(
            "pause", "pause", APPLIED if self.apply else PLANNED, f"{before} -> PAUSED",
            before, "PAUSED",
        )

    def _scale(self, action: ScaleBudgetAction, obj: AdObject) -> ActionResult:
        desc = action.describe()
        budget_field = {
            "auto": obj.budget_field,
            "daily": "daily_budget" if obj.daily_budget else None,
            "lifetime": "lifetime_budget" if obj.lifetime_budget else None,
        }[action.budget]
        if budget_field is None:
            where = "campaign" if obj.level == "adset" else "ad sets"
            return ActionResult(
                "scale_budget", desc, SKIPPED,
                f"no {action.budget if action.budget != 'auto' else ''} budget on this "
                f"{obj.level} (the budget is probably set on the {where})".replace("  ", " "),
            )
        current_minor = int(getattr(obj, budget_field))
        current = to_major(current_minor, self._currency) or 0.0
        target = current * (1 + action.percent / 100.0)
        capped = ""
        if action.max is not None and target > action.max:
            target, capped = action.max, " (capped at max)"
        if action.min is not None and target < action.min:
            target, capped = action.min, " (held at min)"
        new_minor = to_minor(target, self._currency)
        if action.percent > 0 and new_minor <= current_minor:
            return ActionResult(
                "scale_budget", desc, SKIPPED,
                f"already at or above max ({format_money(current, self._currency)})",
                current, current,
            )
        if action.percent < 0 and new_minor >= current_minor:
            return ActionResult(
                "scale_budget", desc, SKIPPED,
                f"already at or below min ({format_money(current, self._currency)})",
                current, current,
            )
        new_major = to_major(new_minor, self._currency)
        if self.apply:
            self.api.set_budget(obj.id, budget_field, new_minor)
        setattr(obj, budget_field, new_minor)
        detail = (
            f"{budget_field} {format_money(current, self._currency)} -> "
            f"{format_money(new_major, self._currency)}{capped}"
        )
        return ActionResult(
            "scale_budget", desc, APPLIED if self.apply else PLANNED, detail, current, new_major
        )

    def _label(self, action: LabelAction, obj: AdObject) -> ActionResult:
        desc = action.describe()
        if action.name.lower() in {lbl.lower() for lbl in obj.labels}:
            return ActionResult("label", desc, SKIPPED, "already labelled")
        if self.apply:
            self.api.add_label(obj.level, obj.id, action.name)
        obj.labels.append(action.name)
        return ActionResult("label", desc, APPLIED if self.apply else PLANNED, action.name)

    def _audit(self, match: Match, result: ActionResult) -> None:
        self.audit.write(
            {
                "run_id": self._run_id,
                "dry_run": not self.apply,
                "rule": match.rule.name,
                "level": match.obj.level,
                "object_id": match.obj.id,
                "object_name": match.obj.name,
                "action": result.kind,
                "status": result.status,
                "detail": result.detail,
                "before": result.before,
                "after": result.after,
                "values": dict(match.values),
            }
        )

    # -- notifications ---------------------------------------------------------

    def render_message(self, match: Match, action: NotifyAction, window_label: str) -> str:
        template = action.message or match.rule.message or DEFAULT_MESSAGE
        acted = [
            f"{r.description} [{r.status}]" for r in match.results if r.kind != "notify"
        ]
        context: dict[str, Any] = {
            **match.metrics,
            **match.rule.vars,
            "rule": match.rule.name,
            "description": match.rule.description,
            "level": match.obj.level,
            "id": match.obj.id,
            "name": match.obj.name,
            "status": match.obj.status,
            "currency": self._currency or "",
            "window": window_label,
            "actions": ", ".join(acted) or "notify only",
            "condition": match.rule.condition.source,
            "values": ", ".join(f"{k}={_fmt(v)}" for k, v in match.values),
        }
        try:
            return _Formatter().format(template, **context)
        except (ValueError, IndexError, AttributeError) as exc:
            return f"{match.rule.name}: invalid message template ({exc}); {context['values']}"

    def _send_notifications(self, report: RunReport) -> None:
        if not self._notifications or not (self.apply or self.notify_in_dry_run):
            return
        prefix = "" if self.apply else "[DRY RUN] "
        account = report.account_name or report.account_id
        per_channel: dict[str, list[Notification]] = {}
        for note in self._notifications:
            targets = note.channels or tuple(self.notifiers)
            for name in targets:
                per_channel.setdefault(name, []).append(note)
        for name, notes in per_channel.items():
            notifier = self.notifiers.get(name)
            if notifier is None:
                report.errors.append(f"notifier {name!r} is not configured")
                continue
            if self.config.notify_mode == "each":
                batches = [(f"{prefix}{n.rule.name}: {n.obj.name}", [n]) for n in notes]
            else:
                subject = f"{prefix}{len(notes)} alert(s) on {account}"
                batches = [(subject, notes)]
            for subject, batch in batches:
                text = "\n\n".join(f"- {n.text}" for n in batch)
                try:
                    notifier.send(subject, text)
                    report.notifications_sent += 1
                except (NotifyError, MetaAdsGuardError, OSError) as exc:
                    report.errors.append(f"notifier {name!r}: {exc}")

    # -- run -------------------------------------------------------------------

    def run(self, only: Optional[list[str]] = None) -> RunReport:
        self._run_id = uuid.uuid4().hex[:12]
        started = self._now()
        report = RunReport(
            self._run_id, started, not self.apply, self.api.account_id,
            currency=self.config.currency,
        )
        calls_before = self.api.client.calls
        self._load_account(report)
        rules = [r for r in self.config.rules if r.enabled]
        if only:
            unknown = set(only) - {r.name for r in self.config.rules}
            if unknown:
                raise ConfigError(f"unknown rule(s): {sorted(unknown)}")
            rules = [r for r in self.config.rules if r.name in only]
        today = started.astimezone(self._tz).date()
        for rule in rules:
            try:
                matches = self.evaluate_rule(rule, report)
            except GraphAPIError as exc:
                report.errors.append(f"rule {rule.name!r}: {exc}")
                continue
            label = resolve_window(rule.window, today).label()
            for match in matches:
                self._execute(match, report, label)
                report.matches.append(match)
        self._send_notifications(report)
        report.api_calls = self.api.client.calls - calls_before
        report.finished_at = self._now()
        return report


_MISSING = object()


def object_metric(obj: AdObject, name: str, currency: Optional[str], now: datetime) -> Any:
    if name == "daily_budget":
        return to_major(obj.daily_budget, currency)
    if name == "lifetime_budget":
        return to_major(obj.lifetime_budget, currency)
    if name == "budget":
        return to_major(obj.daily_budget or obj.lifetime_budget, currency)
    if name in {"age_hours", "age_days"}:
        if obj.created_time is None:
            return None
        hours = (now - obj.created_time).total_seconds() / 3600.0
        return hours if name == "age_hours" else hours / 24.0
    if name == "hours_since_update":
        if obj.updated_time is None:
            return None
        return (now - obj.updated_time).total_seconds() / 3600.0
    return _MISSING


def _fmt(value: Value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    return pretty_number(float(value))
