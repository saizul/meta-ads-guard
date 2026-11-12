"""Cooldown state, stored in a JSON file or an SQLite database.

The backend is chosen from the file extension: ``.json`` uses a JSON file
(rewritten atomically), ``.db``, ``.sqlite`` and ``.sqlite3`` use SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

SCHEMA_VERSION = 1


@dataclass
class CooldownEntry:
    key: str
    at: datetime
    rule: str
    object_id: str
    actions: list[str]


def cooldown_key(scope: str, rule: str, object_id: str) -> str:
    """``rule`` scope: per rule and object. ``object`` scope: any rule."""
    return f"*|{object_id}" if scope == "object" else f"{rule}|{object_id}"


class StateStore(Protocol):
    def last_action(self, key: str) -> Optional[datetime]: ...

    def record(self, key: str, at: datetime, rule: str, object_id: str, actions: list[str]) -> None:
        ...

    def entries(self) -> list[CooldownEntry]: ...

    def clear(self, rule: Optional[str] = None) -> int: ...

    def close(self) -> None: ...


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MemoryState:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def last_action(self, key: str) -> Optional[datetime]:
        item = self._data.get(key)
        return _parse(item["at"]) if item else None

    def record(self, key: str, at: datetime, rule: str, object_id: str, actions: list[str]) -> None:
        self._data[key] = {
            "at": _iso(at),
            "rule": rule,
            "object_id": object_id,
            "actions": list(actions),
        }

    def entries(self) -> list[CooldownEntry]:
        return sorted(
            (
                CooldownEntry(k, _parse(v["at"]), v["rule"], v["object_id"], v["actions"])
                for k, v in self._data.items()
            ),
            key=lambda e: e.at,
        )

    def clear(self, rule: Optional[str] = None) -> int:
        keys = [k for k, v in self._data.items() if rule is None or v["rule"] == rule]
        for k in keys:
            del self._data[k]
        return len(keys)

    def close(self) -> None:
        return None


class JsonState(MemoryState):
    """Cooldowns in a JSON document, written via a temp file and rename."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8").strip()
            if text:
                doc = json.loads(text)
                self._data = dict(doc.get("cooldowns", {}))
        self._dirty = False

    def record(self, key: str, at: datetime, rule: str, object_id: str, actions: list[str]) -> None:
        super().record(key, at, rule, object_id, actions)
        self._dirty = True
        self.flush()

    def clear(self, rule: Optional[str] = None) -> int:
        n = super().clear(rule)
        self._dirty = True
        self.flush()
        return n

    def flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"version": SCHEMA_VERSION, "cooldowns": self._data}
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._dirty = False


class SqliteState:
    def __init__(self, path: Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldowns (
                key TEXT PRIMARY KEY,
                at TEXT NOT NULL,
                rule TEXT NOT NULL,
                object_id TEXT NOT NULL,
                actions TEXT NOT NULL
            )
            """
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.execute(
            "INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
        )
        self.conn.commit()

    def last_action(self, key: str) -> Optional[datetime]:
        row = self.conn.execute("SELECT at FROM cooldowns WHERE key = ?", (key,)).fetchone()
        return _parse(row[0]) if row else None

    def record(self, key: str, at: datetime, rule: str, object_id: str, actions: list[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cooldowns (key, at, rule, object_id, actions) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, _iso(at), rule, object_id, json.dumps(actions)),
        )
        self.conn.commit()

    def entries(self) -> list[CooldownEntry]:
        rows = self.conn.execute(
            "SELECT key, at, rule, object_id, actions FROM cooldowns ORDER BY at"
        ).fetchall()
        return [CooldownEntry(r[0], _parse(r[1]), r[2], r[3], json.loads(r[4])) for r in rows]

    def clear(self, rule: Optional[str] = None) -> int:
        if rule is None:
            cur = self.conn.execute("DELETE FROM cooldowns")
        else:
            cur = self.conn.execute("DELETE FROM cooldowns WHERE rule = ?", (rule,))
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()


def open_state(path: Optional[str]) -> StateStore:
    if not path:
        return MemoryState()
    p = Path(path).expanduser()
    if p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return SqliteState(p)
    return JsonState(p)
