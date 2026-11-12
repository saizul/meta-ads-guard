"""Rule and action definitions, parsed and validated from YAML data."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional, Union

from .errors import ConfigError, ExpressionError
from .expr import Expression, parse
from .metrics import (
    ACTION_PREFIXES,
    OBJECT_METRICS,
    WINDOW_METRICS,
    is_known_metric,
    resolve_result_types,
)
from .timeutil import canonical_window, parse_duration

LEVELS = ("campaign", "adset", "ad")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_TRAILING_WINDOW = re.compile(
    r"\s+(?:in|over|during|for)\s+(?:the\s+)?(?P<w>(?:last|past)\s+\d+\s+days?|\d+\s*d(?:ays?)?|"
    r"last_\d+d|today|yesterday|this_month|last_month|lifetime)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PauseAction:
    kind: str = "pause"

    def describe(self) -> str:
        return "pause"


@dataclass(frozen=True)
class NotifyAction:
    channels: Optional[tuple[str, ...]] = None
    message: Optional[str] = None
    kind: str = "notify"

    def describe(self) -> str:
        if self.channels:
            return f"notify ({', '.join(self.channels)})"
        return "notify"


@dataclass(frozen=True)
class ScaleBudgetAction:
    percent: float
    min: Optional[float] = None
    max: Optional[float] = None
    budget: str = "auto"  # auto | daily | lifetime
    kind: str = "scale_budget"

    def describe(self) -> str:
        sign = "+" if self.percent >= 0 else ""
        caps = []
        if self.min is not None:
            caps.append(f"min {self.min:g}")
        if self.max is not None:
            caps.append(f"max {self.max:g}")
        suffix = f" ({', '.join(caps)})" if caps else ""
        return f"scale budget {sign}{self.percent:g}%{suffix}"


@dataclass(frozen=True)
class LabelAction:
    name: str
    kind: str = "label"

    def describe(self) -> str:
        return f"label {self.name!r}"


Action = Union[PauseAction, NotifyAction, ScaleBudgetAction, LabelAction]


@dataclass
class Filters:
    effective_status: tuple[str, ...] = ("ACTIVE",)
    name_contains: tuple[str, ...] = ()
    name_not_contains: tuple[str, ...] = ()
    name_regex: Optional[re.Pattern[str]] = None
    ids: tuple[str, ...] = ()
    exclude_ids: tuple[str, ...] = ()
    campaign_ids: tuple[str, ...] = ()
    labels_any: tuple[str, ...] = ()
    labels_none: tuple[str, ...] = ()

    def matches(self, obj: Any) -> bool:
        if self.effective_status and obj.effective_status not in self.effective_status:
            return False
        name = (obj.name or "").lower()
        if self.name_contains and not any(s.lower() in name for s in self.name_contains):
            return False
        if any(s.lower() in name for s in self.name_not_contains):
            return False
        if self.name_regex and not self.name_regex.search(obj.name or ""):
            return False
        if self.ids and obj.id not in self.ids:
            return False
        if obj.id in self.exclude_ids:
            return False
        if self.campaign_ids:
            campaign = obj.id if obj.level == "campaign" else obj.campaign_id
            if campaign not in self.campaign_ids:
                return False
        labels = {lbl.lower() for lbl in obj.labels}
        if self.labels_any and not labels & {lbl.lower() for lbl in self.labels_any}:
            return False
        return not labels & {lbl.lower() for lbl in self.labels_none}


@dataclass
class Rule:
    name: str
    level: str
    condition: Expression
    actions: list[Action]
    window: str = "3d"
    cooldown: timedelta = timedelta(hours=6)
    result: tuple[str, ...] = ()
    result_name: str = "purchases"
    vars: dict[str, float] = field(default_factory=dict)
    filters: Filters = field(default_factory=Filters)
    description: str = ""
    message: Optional[str] = None
    enabled: bool = True

    def windows(self) -> list[str]:
        """Canonical windows this rule needs insights for."""
        needed: set[str] = {self.window}  # reports always show the default window
        for var in self.condition.variables():
            lowered = var.name.lower()
            if lowered in self.vars or lowered in OBJECT_METRICS:
                continue
            needed.add(canonical_window(var.window) if var.window else self.window)
        return sorted(needed)


def _as_tuple(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int)):
        return (str(value),)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    raise ConfigError(f"{what} must be a string or a list")


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{what} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{what} must be a number, got {value!r}") from exc


def parse_action(raw: Any, where: str) -> Action:
    if isinstance(raw, str):
        kind, args = raw.strip(), {}
    elif isinstance(raw, dict) and "type" in raw:
        args = {k: v for k, v in raw.items() if k != "type"}
        kind = str(raw["type"])
    elif isinstance(raw, dict) and len(raw) == 1:
        kind, args = next(iter(raw.items()))
        kind = str(kind)
    else:
        raise ConfigError(f"{where}: an action is a name or a single-key mapping, got {raw!r}")
    kind = kind.lower().replace("-", "_")

    if kind == "pause":
        return PauseAction()
    if kind == "notify":
        if args is None:
            args = {}
        if isinstance(args, (str, list)):
            args = {"channels": args}
        if not isinstance(args, dict):
            raise ConfigError(f"{where}: notify options must be a mapping")
        channels = _as_tuple(args.get("channels") or args.get("channel"), f"{where}.channels")
        return NotifyAction(channels or None, args.get("message"))
    if kind in {"scale_budget", "scale", "budget"}:
        if isinstance(args, (int, float)) and not isinstance(args, bool):
            args = {"percent": args}
        if not isinstance(args, dict) or "percent" not in args:
            raise ConfigError(f"{where}: scale_budget needs percent")
        percent = _number(args["percent"], f"{where}.percent")
        if percent == 0 or percent <= -100:
            raise ConfigError(f"{where}: percent must be non-zero and greater than -100")
        lo = _number(args["min"], f"{where}.min") if args.get("min") is not None else None
        hi = _number(args["max"], f"{where}.max") if args.get("max") is not None else None
        if lo is not None and hi is not None and lo > hi:
            raise ConfigError(f"{where}: min ({lo:g}) is greater than max ({hi:g})")
        if lo is not None and lo < 0:
            raise ConfigError(f"{where}: min cannot be negative")
        budget = str(args.get("budget", "auto")).lower()
        if budget not in {"auto", "daily", "lifetime"}:
            raise ConfigError(f"{where}: budget must be auto, daily or lifetime")
        return ScaleBudgetAction(percent, lo, hi, budget)
    if kind == "label":
        name = args.get("name") if isinstance(args, dict) else args
        if not name or not isinstance(name, str):
            raise ConfigError(f"{where}: label needs a name")
        return LabelAction(name.strip())
    raise ConfigError(f"{where}: unknown action {kind!r} (pause, notify, scale_budget, label)")


def split_trailing_window(condition: str) -> tuple[str, Optional[str]]:
    """``"spend > 10 in last 3 days"`` -> ``("spend > 10", "3d")``."""
    m = _TRAILING_WINDOW.search(condition)
    if not m:
        return condition, None
    return condition[: m.start()].rstrip(), canonical_window(m.group("w"))


def _check_references(expr: Expression, rule_vars: dict[str, float], where: str) -> None:
    known = sorted({*WINDOW_METRICS, *OBJECT_METRICS, *rule_vars})
    for var in expr.variables():
        lowered = var.name.lower()
        if lowered in rule_vars:
            if var.window:
                raise ExpressionError(
                    f"{where}: variable {var.name!r} cannot take a window", expr.source, var.pos
                )
            continue
        if not is_known_metric(lowered):
            prefix = lowered.partition(".")[0]
            if prefix in ACTION_PREFIXES:
                hint = f" (write {prefix}.<action_type>)"
            else:
                close = difflib.get_close_matches(lowered, known, n=1)
                hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise ExpressionError(
                f"{where}: unknown metric or variable {var.name!r}{hint}", expr.source, var.pos
            )
        if lowered in OBJECT_METRICS and var.window:
            raise ExpressionError(
                f"{where}: {var.name!r} is an object field and takes no window",
                expr.source,
                var.pos,
            )
        if var.window:
            try:
                canonical_window(var.window)
            except ConfigError as exc:
                raise ExpressionError(f"{where}: {exc}", expr.source, var.pos) from exc


def parse_rule(
    raw: Any,
    index: int,
    *,
    defaults: dict[str, Any],
    global_vars: dict[str, float],
) -> Rule:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    allowed = {
        "name", "description", "enabled", "level", "when", "window", "result", "vars",
        "filters", "actions", "cooldown", "message",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")

    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ConfigError(
            f"{where}: name is required (letters, digits, '.', '_' and '-', up to 80 chars)"
        )
    where = f"rule {name!r}"

    level = str(raw.get("level", defaults.get("level", "adset"))).lower()
    if level not in LEVELS:
        raise ConfigError(f"{where}: level must be one of {', '.join(LEVELS)}")

    when = raw.get("when")
    if isinstance(when, list):
        if not when:
            raise ConfigError(f"{where}: when is empty")
        parts = [f"({w!s})" for w in when]
        when = " and ".join(parts)
    if not isinstance(when, str) or not when.strip():
        raise ConfigError(f"{where}: when (the condition) is required")
    when, trailing_window = split_trailing_window(when.strip())

    window = raw.get("window")
    if (
        window is not None
        and trailing_window is not None
        and canonical_window(window) != trailing_window
    ):
        raise ConfigError(f"{where}: window and the 'in ...' clause in when disagree")
    window = canonical_window(window or trailing_window or defaults.get("window", "3d"))

    rule_vars = {k.lower(): v for k, v in global_vars.items()}
    raw_vars = raw.get("vars") or {}
    if not isinstance(raw_vars, dict):
        raise ConfigError(f"{where}: vars must be a mapping")
    for key, value in raw_vars.items():
        rule_vars[str(key).lower()] = _number(value, f"{where}.vars.{key}")
    for key in rule_vars:
        if is_known_metric(key):
            raise ConfigError(f"{where}: variable {key!r} shadows a metric of the same name")

    condition = parse(when)
    _check_references(condition, rule_vars, where)

    result_name = raw.get("result", defaults.get("result", "purchases"))
    result = resolve_result_types(result_name)
    if not result:
        raise ConfigError(f"{where}: result is empty")

    raw_actions = raw.get("actions")
    if raw_actions is None:
        raise ConfigError(f"{where}: actions are required")
    if not isinstance(raw_actions, list):
        raw_actions = [raw_actions]
    if not raw_actions:
        raise ConfigError(f"{where}: actions are required")
    actions = [parse_action(a, f"{where}.actions[{i}]") for i, a in enumerate(raw_actions)]
    kinds = [a.kind for a in actions]
    if "scale_budget" in kinds and level == "ad":
        raise ConfigError(f"{where}: ads have no budget; use level adset or campaign")
    if kinds.count("scale_budget") > 1 or kinds.count("pause") > 1:
        raise ConfigError(f"{where}: pause and scale_budget may appear once per rule")
    if "pause" in kinds and "scale_budget" in kinds:
        raise ConfigError(f"{where}: pausing and scaling in the same rule makes no sense")

    filters = parse_filters(raw.get("filters"), defaults, where)
    cooldown = parse_duration(raw.get("cooldown", defaults.get("cooldown", "6h")))
    message = raw.get("message")
    if message is not None and not isinstance(message, str):
        raise ConfigError(f"{where}: message must be a string")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: enabled must be true or false")

    return Rule(
        name=name,
        level=level,
        condition=condition,
        actions=actions,
        window=window,
        cooldown=cooldown,
        result=result,
        result_name=result_name if isinstance(result_name, str) else "results",
        vars=rule_vars,
        filters=filters,
        description=str(raw.get("description") or ""),
        message=message,
        enabled=enabled,
    )


def parse_filters(raw: Any, defaults: dict[str, Any], where: str) -> Filters:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: filters must be a mapping")
    allowed = {
        "effective_status", "name_contains", "name_not_contains", "name_regex", "ids",
        "exclude_ids", "campaign_ids", "labels_any", "labels_none",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown filter keys {sorted(unknown)}")
    status = raw.get("effective_status", defaults.get("effective_status", ["ACTIVE"]))
    pattern = raw.get("name_regex")
    regex = None
    if pattern:
        try:
            regex = re.compile(str(pattern), re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(f"{where}: invalid name_regex: {exc}") from exc
    return Filters(
        effective_status=tuple(s.upper() for s in _as_tuple(status, f"{where}.effective_status")),
        name_contains=_as_tuple(raw.get("name_contains"), f"{where}.name_contains"),
        name_not_contains=_as_tuple(raw.get("name_not_contains"), f"{where}.name_not_contains"),
        name_regex=regex,
        ids=_as_tuple(raw.get("ids"), f"{where}.ids"),
        exclude_ids=_as_tuple(raw.get("exclude_ids"), f"{where}.exclude_ids"),
        campaign_ids=_as_tuple(raw.get("campaign_ids"), f"{where}.campaign_ids"),
        labels_any=_as_tuple(raw.get("labels_any"), f"{where}.labels_any"),
        labels_none=_as_tuple(raw.get("labels_none"), f"{where}.labels_none"),
    )
