"""Use meta-ads-guard as a library: load a config, run it, inspect the report.

    export META_ACCESS_TOKEN=...
    python examples/library_usage.py examples/guard.example.yaml
"""

from __future__ import annotations

import sys

from meta_ads_guard.audit import AuditLog
from meta_ads_guard.cli import build_api
from meta_ads_guard.config import build_notifiers, load_config
from meta_ads_guard.engine import Engine
from meta_ads_guard.state import open_state


def main(path: str) -> int:
    config = load_config(path)
    api = build_api(config)
    state = open_state(config.state_path)
    try:
        engine = Engine(
            config,
            api,
            state=state,
            audit=AuditLog(config.audit_path),
            notifiers=build_notifiers(config),
            apply=False,  # dry run: plan only
        )
        report = engine.run()
    finally:
        state.close()

    for match in report.matches:
        planned = ", ".join(f"{r.description} ({r.status})" for r in match.results)
        print(f"{match.rule.name}: {match.obj.level} {match.obj.name} -> {planned or match.status}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "guard.yaml"))
