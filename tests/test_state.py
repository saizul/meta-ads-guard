import json
from datetime import datetime, timedelta, timezone

import pytest

from meta_ads_guard.audit import AuditLog, read_audit
from meta_ads_guard.state import JsonState, MemoryState, SqliteState, cooldown_key, open_state

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["json", "sqlite", "memory"])
def store(request, tmp_path):
    if request.param == "json":
        return JsonState(tmp_path / "state.json")
    if request.param == "sqlite":
        return SqliteState(tmp_path / "state.sqlite")
    return MemoryState()


def test_record_and_read(store):
    key = cooldown_key("rule", "r1", "123")
    assert store.last_action(key) is None
    store.record(key, NOW, "r1", "123", ["pause", "notify"])
    assert store.last_action(key) == NOW
    later = NOW + timedelta(hours=1)
    store.record(key, later, "r1", "123", ["notify"])
    assert store.last_action(key) == later
    entries = store.entries()
    assert len(entries) == 1 and entries[0].actions == ["notify"]
    store.record(cooldown_key("rule", "r2", "9"), NOW, "r2", "9", ["label"])
    assert store.clear("r1") == 1
    assert [e.rule for e in store.entries()] == ["r2"]
    assert store.clear() == 1
    store.close()


def test_naive_datetimes_are_utc(store):
    store.record("k", datetime(2026, 1, 1, 12, 0), "r", "o", [])
    assert store.last_action("k") == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_object_scope_key():
    assert cooldown_key("object", "r1", "5") == cooldown_key("object", "r2", "5")
    assert cooldown_key("rule", "r1", "5") != cooldown_key("rule", "r2", "5")


def test_json_state_persists_atomically(tmp_path):
    path = tmp_path / "nested" / "state.json"
    s = JsonState(path)
    s.record("r|1", NOW, "r", "1", ["pause"])
    doc = json.loads(path.read_text())
    assert doc["version"] == 1 and "r|1" in doc["cooldowns"]
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]
    assert JsonState(path).last_action("r|1") == NOW


def test_json_state_empty_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("")
    assert JsonState(path).entries() == []


def test_sqlite_state_persists(tmp_path):
    path = tmp_path / "s.db"
    s = SqliteState(path)
    s.record("r|1", NOW, "r", "1", ["pause"])
    s.close()
    s2 = SqliteState(path)
    assert s2.last_action("r|1") == NOW
    s2.close()


def test_open_state_by_extension(tmp_path):
    assert isinstance(open_state(None), MemoryState)
    assert isinstance(open_state(str(tmp_path / "a.json")), JsonState)
    for ext in ("db", "sqlite", "sqlite3"):
        s = open_state(str(tmp_path / f"a.{ext}"))
        assert isinstance(s, SqliteState)
        s.close()


def test_audit_log(tmp_path):
    path = tmp_path / "logs" / "audit.jsonl"
    log = AuditLog(str(path))
    log.write({"rule": "r", "action": "pause", "dry_run": False, "detail": "টাকা"})
    log.write({"rule": "r", "action": "notify", "dry_run": True})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "টাকা" in lines[0]
    entries = list(read_audit(str(path)))
    assert entries[0]["action"] == "pause" and "ts" in entries[0]
    assert [e["action"] for e in read_audit(str(path), limit=1)] == ["notify"]


def test_audit_skip_dry_run(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(str(path), include_dry_run=False)
    log.write({"dry_run": True})
    assert not path.exists()
    assert len(log.records) == 1


def test_read_audit_skips_bad_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"a": 1}\nnot json\n\n{"a": 2}\n')
    assert [e["a"] for e in read_audit(str(path))] == [1, 2]
    assert list(read_audit(str(tmp_path / "missing.jsonl"))) == []


def test_memory_audit():
    log = AuditLog(None)
    log.write({"x": 1})
    assert log.records[0]["x"] == 1
