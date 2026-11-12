"""Command-line interface: ``meta-ads-guard <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Optional, TextIO

from . import __version__
from .audit import AuditLog, read_audit
from .config import Config, build_notifiers, load_config
from .engine import Engine
from .errors import AuthError, ConfigError, GraphAPIError, MetaAdsGuardError
from .graph import GraphClient
from .marketing import LEVELS, MarketingAPI
from .metrics import (
    OBJECT_METRICS,
    RESULT_ACTION_TYPES,
    WINDOW_METRICS,
    compute_metrics,
    group_rows,
    resolve_result_types,
)
from .ratelimit import RateLimiter
from .report import render_run, table
from .state import open_state
from .timeutil import get_timezone, resolve_window, utcnow

EXIT_OK, EXIT_ERRORS, EXIT_CONFIG, EXIT_AUTH = 0, 1, 2, 3


def build_api(
    config: Config, env: Optional[Mapping[str, str]] = None, transport: Any = None
) -> MarketingAPI:
    env = os.environ if env is None else env
    token = env.get(config.api.access_token_env)
    if not token:
        raise ConfigError(
            f"environment variable {config.api.access_token_env} (the access token) is not set"
        )
    secret = env.get(config.api.app_secret_env) if config.api.app_secret_env else None
    client = GraphClient(
        token,
        api_version=config.api.version,
        app_secret=secret,
        transport=transport,
        rate_limiter=RateLimiter(
            threshold=config.api.usage_threshold, max_wait=config.api.max_wait
        ),
        timeout=config.api.timeout,
        max_retries=config.api.max_retries,
    )
    return MarketingAPI(
        client,
        config.account_id,
        insights_mode=config.insights.mode,
        async_threshold_days=config.insights.async_threshold_days,
        poll_interval=config.insights.poll_interval,
        poll_timeout=config.insights.poll_timeout,
        attribution_windows=config.insights.attribution_windows,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meta-ads-guard",
        description="Rules engine for Meta ad accounts. Dry-run by default.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-c", "--config", default=os.environ.get("META_ADS_GUARD_CONFIG", "guard.yaml"),
            help="YAML config file (default: guard.yaml or $META_ADS_GUARD_CONFIG)",
        )

    run = sub.add_parser("run", help="evaluate rules and plan (or apply) actions")
    with_config(run)
    run.add_argument("--apply", action="store_true", help="really change objects and notify")
    run.add_argument("--rule", action="append", help="only run this rule (repeatable)")
    run.add_argument(
        "--notify", action="store_true", help="send notifications during a dry run, marked"
    )
    run.add_argument("--json", action="store_true", help="print the report as JSON")

    val = sub.add_parser("validate", help="check the config file without calling the API")
    with_config(val)

    ins = sub.add_parser("insights", help="print metrics per object for a window")
    with_config(ins)
    ins.add_argument("--level", choices=LEVELS, default="adset")
    ins.add_argument("--window", default="7d")
    ins.add_argument("--result", default="purchases", help="result alias or action type")
    ins.add_argument("--sort", default="spend", help="metric to sort by (descending)")
    ins.add_argument("--top", type=int, default=25)
    ins.add_argument("--json", action="store_true")

    cd = sub.add_parser("cooldowns", help="list or clear cooldown state")
    with_config(cd)
    cd.add_argument("--clear", action="store_true", help="remove cooldowns")
    cd.add_argument("--rule", help="limit --clear to one rule")

    au = sub.add_parser("audit", help="show the latest audit log entries")
    with_config(au)
    au.add_argument("--tail", type=int, default=20)
    au.add_argument("--json", action="store_true")

    sub.add_parser("metrics", help="list the metric names rules can use")
    return parser


def cmd_validate(config: Config, out: TextIO) -> int:
    print(f"config OK: {len(config.rules)} rule(s) for {config.account_id}", file=out)
    rows = []
    for r in config.rules:
        rows.append([
            r.name, r.level, r.window, ", ".join(a.describe() for a in r.actions),
            "off" if not r.enabled else str(r.cooldown), r.condition.source,
        ])
    print(table(["rule", "level", "window", "actions", "cooldown", "when"], rows), file=out)
    if config.notifiers:
        print("notifiers: " + ", ".join(
            f"{k} ({v['type']})" for k, v in config.notifiers.items()), file=out)
    for w in config.warnings:
        print(f"warning: {w}", file=out)
    return EXIT_OK


def cmd_insights(config: Config, args: argparse.Namespace, api: MarketingAPI, out: TextIO) -> int:
    tz = get_timezone(config.timezone)
    window = resolve_window(args.window, utcnow().astimezone(tz).date())
    rows = api.insights(args.level, window)
    grouped = group_rows(rows, args.level)
    result_types = resolve_result_types(args.result)
    items = []
    for oid, totals in grouped.items():
        m = compute_metrics(totals, result_types)
        items.append({"id": oid, "name": totals.name, **m})
    items.sort(key=lambda d: d.get(args.sort) or 0, reverse=True)
    items = items[: args.top]
    if args.json:
        json.dump({"window": window.label(), "level": args.level, "rows": items}, out, indent=2)
        out.write("\n")
        return EXIT_OK
    cols = ["spend", "impressions", "clicks", "ctr", "cpc", "cpm", "frequency", "results",
            "cpa", "roas"]
    print(f"{args.level} insights, {window.label()}, results = {args.result}", file=out)
    print(table(["id", "name", *cols], [[d["id"], d["name"]] + [d.get(c) for c in cols]
                                        for d in items]), file=out)
    return EXIT_OK


def cmd_metrics(out: TextIO) -> int:
    print("Window metrics (use name or name[window], e.g. spend[7d]):", file=out)
    print(table(["metric", "meaning"], sorted(WINDOW_METRICS.items())), file=out)
    print("\nObject fields (no window):", file=out)
    print(table(["field", "meaning"], sorted(OBJECT_METRICS.items())), file=out)
    print("\nRaw actions: actions.<action_type>, action_values.<action_type>, "
          "cost_per_action.<action_type>", file=out)
    print("\nResult aliases:", file=out)
    print(table(["alias", "action types (first present wins)"],
                [[k, ", ".join(v)] for k, v in RESULT_ACTION_TYPES.items()]), file=out)
    return EXIT_OK


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    transport: Any = None,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    level = logging.WARNING - 10 * min(args.verbose, 2)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=err)

    if args.command == "metrics":
        return cmd_metrics(out)

    try:
        config = load_config(args.config, env=env)
        if args.command == "validate":
            return cmd_validate(config, out)
        if args.command == "audit":
            if not config.audit_path:
                print("no audit.path configured", file=err)
                return EXIT_CONFIG
            entries = list(read_audit(config.audit_path, limit=args.tail))
            if args.json:
                json.dump(entries, out, indent=2, ensure_ascii=False)
                out.write("\n")
            else:
                print(table(
                    ["ts", "dry", "rule", "object", "action", "status", "detail"],
                    [[e.get("ts", "")[:19], e.get("dry_run"), e.get("rule"),
                      e.get("object_name") or e.get("object_id"), e.get("action"),
                      e.get("status"), e.get("detail")] for e in entries],
                ), file=out)
            return EXIT_OK
        if args.command == "cooldowns":
            state = open_state(config.state_path)
            try:
                if args.clear:
                    n = state.clear(args.rule)
                    print(f"cleared {n} cooldown(s)", file=out)
                else:
                    print(table(["rule", "object", "last action (UTC)", "actions"],
                                [[e.rule, e.object_id, e.at.isoformat()[:19], ",".join(e.actions)]
                                 for e in state.entries()]), file=out)
            finally:
                state.close()
            return EXIT_OK

        api = build_api(config, env=env, transport=transport)
        if args.command == "insights":
            return cmd_insights(config, args, api, out)

        for w in config.warnings:
            print(f"warning: {w}", file=err)
        notifiers = build_notifiers(config, env=env, transport=transport)
        state = open_state(config.state_path)
        try:
            engine = Engine(
                config,
                api,
                state=state,
                audit=AuditLog(config.audit_path, include_dry_run=config.audit_dry_run),
                notifiers=notifiers,
                apply=args.apply,
                notify_in_dry_run=args.notify,
            )
            report = engine.run(args.rule)
        finally:
            state.close()
        if args.json:
            json.dump(report.to_dict(), out, indent=2, ensure_ascii=False, default=str)
            out.write("\n")
        else:
            print(render_run(report), file=out)
        return EXIT_OK if report.ok else EXIT_ERRORS
    except ConfigError as exc:
        print(f"config error: {exc}", file=err)
        return EXIT_CONFIG
    except AuthError as exc:
        print(f"authentication error: {exc}", file=err)
        return EXIT_AUTH
    except (GraphAPIError, MetaAdsGuardError) as exc:
        print(f"error: {exc}", file=err)
        return EXIT_ERRORS


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
