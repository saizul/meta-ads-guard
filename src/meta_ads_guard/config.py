"""Load and validate the YAML configuration file."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .errors import ConfigError
from .graph import DEFAULT_API_VERSION, normalize_account_id
from .notify import EmailNotifier, Notifier, TelegramNotifier, WebhookNotifier
from .rules import ScaleBudgetAction, parse_rule
from .timeutil import canonical_window, get_timezone, parse_duration

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_VERSION_RE = re.compile(r"^v\d+\.\d+$")
TOP_LEVEL_KEYS = {
    "account", "api", "insights", "state", "audit", "safety", "defaults", "vars",
    "notifiers", "notify", "rules",
}


@dataclass
class ApiSettings:
    version: str = DEFAULT_API_VERSION
    access_token_env: str = "META_ACCESS_TOKEN"
    app_secret_env: Optional[str] = None
    timeout: float = 30.0
    max_retries: int = 5
    usage_threshold: float = 85.0
    max_wait: float = 900.0


@dataclass
class InsightsSettings:
    mode: str = "auto"
    async_threshold_days: int = 30
    poll_interval: float = 5.0
    poll_timeout: float = 1800.0
    attribution_windows: Optional[list[str]] = None


@dataclass
class Config:
    account_id: str
    rules: list[Any]
    currency: Optional[str] = None
    timezone: Optional[str] = None
    api: ApiSettings = field(default_factory=ApiSettings)
    insights: InsightsSettings = field(default_factory=InsightsSettings)
    state_path: Optional[str] = None
    cooldown_scope: str = "rule"
    audit_path: Optional[str] = None
    audit_dry_run: bool = True
    max_actions_per_run: int = 25
    max_budget_increase_pct: float = 50.0
    max_budget_decrease_pct: float = 50.0
    notifiers: dict[str, dict[str, Any]] = field(default_factory=dict)
    notify_mode: str = "digest"
    vars: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source: Optional[Path] = None

    def rule(self, name: str) -> Any:
        for r in self.rules:
            if r.name == name:
                return r
        raise KeyError(name)


def interpolate(value: Any, env: Mapping[str, str], where: str = "") -> Any:
    """Replace ``${VAR}`` and ``${VAR:-default}`` in every string of ``value``."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"{where or 'config'}: environment variable {name} is not set")

        return _ENV_REF.sub(sub, value)
    if isinstance(value, list):
        return [interpolate(v, env, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: interpolate(v, env, f"{where}.{k}" if where else str(k)) for k, v in
                value.items()}
    return value


def _section(doc: dict[str, Any], key: str) -> dict[str, Any]:
    value = doc.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _check_keys(section: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")


def _num(value: Any, where: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{where} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} must be a number, got {value!r}") from exc
    if number < minimum:
        raise ConfigError(f"{where} must be at least {minimum:g}")
    return number


def parse_config(
    doc: Any, *, env: Optional[Mapping[str, str]] = None, base_dir: Optional[Path] = None
) -> Config:
    env = os.environ if env is None else env
    if not isinstance(doc, dict):
        raise ConfigError("the configuration must be a YAML mapping")
    _check_keys(doc, TOP_LEVEL_KEYS, "config")
    doc = interpolate(doc, env)
    warnings: list[str] = []

    account = _section(doc, "account")
    _check_keys(account, {"id", "currency", "timezone"}, "account")
    raw_id = account.get("id") or env.get("META_AD_ACCOUNT_ID")
    if not raw_id:
        raise ConfigError("account.id is required (or set META_AD_ACCOUNT_ID)")
    try:
        account_id = normalize_account_id(str(raw_id))
    except ValueError as exc:
        raise ConfigError(f"account.id: {exc}") from exc
    currency = account.get("currency")
    if currency is not None:
        currency = str(currency).upper()
        if not re.match(r"^[A-Z]{3}$", currency):
            raise ConfigError("account.currency must be a 3-letter ISO code such as BDT")
    tz = account.get("timezone")
    if tz:
        get_timezone(str(tz))

    api_raw = _section(doc, "api")
    if "access_token" in api_raw:
        raise ConfigError("api.access_token is not supported; use access_token_env")
    _check_keys(
        api_raw,
        {"version", "access_token_env", "app_secret_env", "timeout", "max_retries",
         "usage_threshold", "max_wait"},
        "api",
    )
    api = ApiSettings()
    if "version" in api_raw:
        api.version = str(api_raw["version"])
        if not _VERSION_RE.match(api.version):
            raise ConfigError("api.version must look like v24.0")
    api.access_token_env = str(api_raw.get("access_token_env", api.access_token_env))
    api.app_secret_env = api_raw.get("app_secret_env")
    api.timeout = _num(api_raw.get("timeout", api.timeout), "api.timeout", minimum=1)
    api.max_retries = int(_num(api_raw.get("max_retries", api.max_retries), "api.max_retries"))
    api.usage_threshold = _num(
        api_raw.get("usage_threshold", api.usage_threshold), "api.usage_threshold", minimum=1
    )
    if api.usage_threshold > 100:
        raise ConfigError("api.usage_threshold is a percentage (1-100)")
    api.max_wait = _num(api_raw.get("max_wait", api.max_wait), "api.max_wait", minimum=1)

    ins_raw = _section(doc, "insights")
    _check_keys(
        ins_raw,
        {"mode", "async_threshold_days", "poll_interval", "poll_timeout", "attribution_windows"},
        "insights",
    )
    insights = InsightsSettings()
    insights.mode = str(ins_raw.get("mode", insights.mode)).lower()
    if insights.mode not in {"auto", "sync", "async"}:
        raise ConfigError("insights.mode must be auto, sync or async")
    insights.async_threshold_days = int(
        _num(ins_raw.get("async_threshold_days", 30), "insights.async_threshold_days", minimum=1)
    )
    insights.poll_interval = _num(
        ins_raw.get("poll_interval", 5), "insights.poll_interval", minimum=0.1
    )
    insights.poll_timeout = _num(
        ins_raw.get("poll_timeout", 1800), "insights.poll_timeout", minimum=1
    )
    aw = ins_raw.get("attribution_windows")
    if aw is not None:
        if isinstance(aw, str):
            aw = [aw]
        if not isinstance(aw, list) or not all(isinstance(x, str) for x in aw):
            raise ConfigError("insights.attribution_windows must be a list such as [7d_click]")
        insights.attribution_windows = list(aw)

    base = base_dir or Path.cwd()

    def resolve_path(value: Any) -> Optional[str]:
        if not value:
            return None
        p = Path(str(value)).expanduser()
        return str(p if p.is_absolute() else base / p)

    state = _section(doc, "state")
    _check_keys(state, {"path", "cooldown_scope"}, "state")
    cooldown_scope = str(state.get("cooldown_scope", "rule"))
    if cooldown_scope not in {"rule", "object"}:
        raise ConfigError("state.cooldown_scope must be rule or object")

    audit = _section(doc, "audit")
    _check_keys(audit, {"path", "include_dry_run"}, "audit")

    safety = _section(doc, "safety")
    _check_keys(
        safety,
        {"max_actions_per_run", "max_budget_increase_pct", "max_budget_decrease_pct"},
        "safety",
    )
    max_actions = int(_num(safety.get("max_actions_per_run", 25), "safety.max_actions_per_run"))
    max_inc = _num(safety.get("max_budget_increase_pct", 50), "safety.max_budget_increase_pct")
    max_dec = _num(safety.get("max_budget_decrease_pct", 50), "safety.max_budget_decrease_pct")

    defaults = _section(doc, "defaults")
    _check_keys(
        defaults, {"window", "cooldown", "result", "effective_status", "level"}, "defaults"
    )
    if "window" in defaults:
        canonical_window(defaults["window"])
    if "cooldown" in defaults:
        parse_duration(defaults["cooldown"])

    raw_vars = _section(doc, "vars")
    global_vars = {str(k).lower(): _num(v, f"vars.{k}", minimum=-1e18) for k, v in
                   raw_vars.items()}

    notifiers_raw = _section(doc, "notifiers")
    for name, spec in notifiers_raw.items():
        validate_notifier(str(name), spec, warnings)
    notify = _section(doc, "notify")
    _check_keys(notify, {"mode"}, "notify")
    notify_mode = str(notify.get("mode", "digest"))
    if notify_mode not in {"digest", "each"}:
        raise ConfigError("notify.mode must be digest or each")

    raw_rules = doc.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigError("rules must be a non-empty list")
    rules = [
        parse_rule(r, i, defaults=defaults, global_vars=global_vars)
        for i, r in enumerate(raw_rules)
    ]
    seen: set[str] = set()
    for rule in rules:
        if rule.name in seen:
            raise ConfigError(f"duplicate rule name {rule.name!r}")
        seen.add(rule.name)
        for action in rule.actions:
            if getattr(action, "channels", None):
                missing = [c for c in action.channels if c not in notifiers_raw]
                if missing:
                    raise ConfigError(f"rule {rule.name!r}: unknown notifier(s) {missing}")
            if isinstance(action, ScaleBudgetAction):
                if action.percent > max_inc:
                    raise ConfigError(
                        f"rule {rule.name!r}: +{action.percent:g}% is above "
                        f"safety.max_budget_increase_pct ({max_inc:g})"
                    )
                if -action.percent > max_dec:
                    raise ConfigError(
                        f"rule {rule.name!r}: {action.percent:g}% is beyond "
                        f"safety.max_budget_decrease_pct ({max_dec:g})"
                    )
                if rule.cooldown.total_seconds() < 3600:
                    warnings.append(
                        f"rule {rule.name!r}: budget cooldown under 1h; Meta blocks more "
                        "than 4 budget changes per ad set per hour (error 613/1487632)"
                    )
                if action.percent > 0 and action.max is None:
                    warnings.append(f"rule {rule.name!r}: scaling up without a max cap")
        if currency:
            other = rule.condition.currencies() - {currency}
            if other:
                raise ConfigError(
                    f"rule {rule.name!r}: condition uses {sorted(other)} but the account "
                    f"currency is {currency}; amounts are not converted"
                )
        if rule.cooldown.total_seconds() == 0 and any(
            a.kind != "notify" for a in rule.actions
        ):
            warnings.append(f"rule {rule.name!r}: no cooldown, it may act on every run")

    if not notifiers_raw and any(a.kind == "notify" for r in rules for a in r.actions):
        warnings.append("rules use notify but no notifiers are configured")

    return Config(
        account_id=account_id,
        rules=rules,
        currency=currency,
        timezone=str(tz) if tz else None,
        api=api,
        insights=insights,
        state_path=resolve_path(state.get("path")),
        cooldown_scope=cooldown_scope,
        audit_path=resolve_path(audit.get("path")),
        audit_dry_run=bool(audit.get("include_dry_run", True)),
        max_actions_per_run=max_actions,
        max_budget_increase_pct=max_inc,
        max_budget_decrease_pct=max_dec,
        notifiers={str(k): dict(v) for k, v in notifiers_raw.items()},
        notify_mode=notify_mode,
        vars=global_vars,
        warnings=warnings,
    )


def load_config(path: str, env: Optional[Mapping[str, str]] = None) -> Config:
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc
    config = parse_config(doc, env=env, base_dir=p.parent.resolve())
    config.source = p
    return config


NOTIFIER_KEYS = {
    "telegram": {"type", "bot_token_env", "chat_id", "disable_notification", "api_url"},
    "webhook": {"type", "url", "url_env", "field", "max_length", "extra"},
    "email": {
        "type", "host", "port", "security", "username_env", "password_env", "from", "to",
        "subject_prefix",
    },
}


def validate_notifier(name: str, spec: Any, warnings: list[str]) -> None:
    where = f"notifiers.{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be a mapping")
    kind = spec.get("type")
    if kind not in NOTIFIER_KEYS:
        raise ConfigError(f"{where}.type must be one of {sorted(NOTIFIER_KEYS)}")
    _check_keys(spec, NOTIFIER_KEYS[kind], where)
    if kind == "telegram":
        if not spec.get("bot_token_env") or not spec.get("chat_id"):
            raise ConfigError(f"{where}: bot_token_env and chat_id are required")
    elif kind == "webhook":
        if not spec.get("url") and not spec.get("url_env"):
            raise ConfigError(f"{where}: url_env (or url) is required")
        if spec.get("url"):
            warnings.append(f"{where}: webhook URLs are secrets; prefer url_env")
    elif kind == "email":
        for key in ("host", "from", "to"):
            if not spec.get(key):
                raise ConfigError(f"{where}: {key} is required")
        if spec.get("security", "starttls") not in {"starttls", "ssl", "none"}:
            raise ConfigError(f"{where}.security must be starttls, ssl or none")
        if spec.get("security") == "none" and spec.get("password_env"):
            warnings.append(f"{where}: sending an SMTP password without TLS")


def _env(env: Mapping[str, str], key: Optional[str], where: str) -> str:
    if not key:
        raise ConfigError(f"{where}: environment variable name missing")
    value = env.get(str(key))
    if not value:
        raise ConfigError(f"{where}: environment variable {key} is not set")
    return value


def build_notifiers(
    config: Config, env: Optional[Mapping[str, str]] = None, transport: Any = None
) -> dict[str, Notifier]:
    env = os.environ if env is None else env
    out: dict[str, Notifier] = {}
    for name, spec in config.notifiers.items():
        where = f"notifiers.{name}"
        kind = spec["type"]
        if kind == "telegram":
            out[name] = TelegramNotifier(
                name,
                _env(env, spec.get("bot_token_env"), where),
                str(spec["chat_id"]),
                transport=transport,
                disable_notification=bool(spec.get("disable_notification", False)),
                **({"api_url": spec["api_url"]} if spec.get("api_url") else {}),
            )
        elif kind == "webhook":
            url = spec.get("url") or _env(env, spec.get("url_env"), where)
            out[name] = WebhookNotifier(
                name,
                url,
                field=str(spec.get("field", "text")),
                max_length=int(spec.get("max_length", 3500)),
                extra=spec.get("extra") or {},
                transport=transport,
            )
        else:
            recipients = spec["to"] if isinstance(spec["to"], list) else [spec["to"]]
            username = env.get(spec["username_env"]) if spec.get("username_env") else None
            password = (
                _env(env, spec["password_env"], where) if spec.get("password_env") else None
            )
            out[name] = EmailNotifier(
                name,
                host=str(spec["host"]),
                port=int(spec.get("port", 587)),
                sender=str(spec["from"]),
                recipients=[str(r) for r in recipients],
                username=username,
                password=password,
                security=str(spec.get("security", "starttls")),
                subject_prefix=str(spec.get("subject_prefix", "[meta-ads-guard]")),
            )
    return out
