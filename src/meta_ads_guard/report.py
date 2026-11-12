"""Plain-text rendering of run reports and tables."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .engine import RunReport, pretty_number


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(h) for h in headers]] + [[_cell(v) for v in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = []
    for n, row in enumerate(cells):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return pretty_number(value)
    text = str(value)
    return text if len(text) <= 72 else text[:69] + "..."


def render_run(report: RunReport) -> str:
    mode = "DRY RUN (nothing changed; add --apply to act)" if report.dry_run else "APPLIED"
    head = [
        f"meta-ads-guard run {report.run_id}: {mode}",
        f"account {report.account_name or '-'} ({report.account_id}), "
        f"currency {report.currency or '?'}, time zone {report.timezone}",
    ]
    checked = ", ".join(f"{k}={v}" for k, v in report.checked.items()) or "none"
    head.append(f"objects checked per rule: {checked}")
    out = ["\n".join(head)]

    if report.matches:
        rows = []
        for m in report.matches:
            values = ", ".join(f"{k}={_value(v)}" for k, v in m.values)
            if m.status != "acted":
                rows.append([m.rule.name, m.obj.level, m.obj.name, m.obj.id, m.status, m.detail,
                             values])
                continue
            for r in m.results:
                rows.append([m.rule.name, m.obj.level, m.obj.name, m.obj.id, r.status,
                             r.description if r.kind == "notify" else
                             f"{r.description}: {r.detail}", values])
        out.append(table(["rule", "level", "name", "id", "status", "action", "values"], rows))
    else:
        out.append("no rule matched")

    counts = report.counts()
    if counts:
        out.append("summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    out.append(
        f"api calls {report.api_calls}, notifications sent {report.notifications_sent}"
    )
    if report.errors:
        out.append("errors:\n" + "\n".join(f"  - {e}" for e in report.errors))
    return "\n\n".join(out)


def _value(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v).lower()
    return pretty_number(float(v))
