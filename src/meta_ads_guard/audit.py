"""Append-only audit log in JSON Lines format.

One line per planned or executed action::

    {"ts": "...", "run_id": "...", "dry_run": true, "rule": "...", "level": "adset",
     "object_id": "...", "object_name": "...", "action": "pause", "status": "planned",
     "detail": "...", "metrics": {...}}
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class AuditLog:
    def __init__(self, path: Optional[str], *, include_dry_run: bool = True):
        self.path = Path(path).expanduser() if path else None
        self.include_dry_run = include_dry_run
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        self.records.append(entry)
        if self.path is None:
            return
        if entry.get("dry_run") and not self.include_dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def read_audit(path: str, *, limit: Optional[int] = None) -> Iterator[dict[str, Any]]:
    """Yield entries from an audit file, newest last. Corrupt lines are skipped."""
    p = Path(path).expanduser()
    if not p.exists():
        return iter(())
    lines = p.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]

    def gen() -> Iterator[dict[str, Any]]:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue

    return gen()
