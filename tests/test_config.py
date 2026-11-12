import textwrap

import pytest

from meta_ads_guard.config import build_notifiers, interpolate, load_config, parse_config
from meta_ads_guard.errors import ConfigError
from meta_ads_guard.notify import EmailNotifier, TelegramNotifier, WebhookNotifier


def minimal(**extra):
    doc = {
        "account": {"id": "act_123", "currency": "BDT", "timezone": "Asia/Dhaka"},
        "rules": [{"name": "r", "when": "spend > 1500 BDT and purchases == 0",
                   "actions": ["pause"]}],
    }
    doc.update(extra)
    return doc


def test_minimal_config(tmp_path):
    cfg = parse_config(minimal(state={"path": "state.json"}), env={}, base_dir=tmp_path)
    assert cfg.account_id == "act_123"
    assert cfg.api.version.startswith("v")
    assert cfg.api.access_token_env == "META_ACCESS_TOKEN"
    assert cfg.state_path == str(tmp_path / "state.json")
    assert cfg.audit_path is None
    assert cfg.rules[0].name == "r"
    assert cfg.rule("r") is cfg.rules[0]
    with pytest.raises(KeyError):
        cfg.rule("nope")


def test_account_id_from_env():
    doc = minimal()
    del doc["account"]["id"]
    assert parse_config(doc, env={"META_AD_ACCOUNT_ID": "999"}).account_id == "act_999"
    with pytest.raises(ConfigError, match=r"account\.id"):
        parse_config(doc, env={})


def test_env_interpolation():
    assert interpolate({"a": "${X}", "b": ["${Y:-dflt}"], "c": 3}, {"X": "1"}) == {
        "a": "1", "b": ["dflt"], "c": 3}
    with pytest.raises(ConfigError, match="MISSING"):
        interpolate({"a": "${MISSING}"}, {})
    doc = minimal()
    doc["account"]["id"] = "${ACC}"
    assert parse_config(doc, env={"ACC": "act_42"}).account_id == "act_42"


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"colour": 1}, "unknown keys"),
        ({"api": {"version": "24.0"}}, "api.version"),
        ({"api": {"access_token": "abc"}}, "not supported"),
        ({"api": {"usage_threshold": 150}}, "percentage"),
        ({"api": {"timeout": 0}}, "at least"),
        ({"insights": {"mode": "turbo"}}, "insights.mode"),
        ({"insights": {"attribution_windows": [1]}}, "attribution_windows"),
        ({"state": {"cooldown_scope": "global"}}, "cooldown_scope"),
        ({"notify": {"mode": "loud"}}, "notify.mode"),
        ({"defaults": {"window": "fortnight"}}, "unknown window"),
        ({"defaults": {"cooldown": "soon"}}, "invalid duration"),
        ({"rules": []}, "non-empty"),
        ({"rules": [{"name": "a", "when": "spend > 1", "actions": ["pause"]},
                    {"name": "a", "when": "spend > 1", "actions": ["pause"]}]}, "duplicate"),
        ({"rules": [{"name": "a", "when": "spend > 1 USD", "actions": ["pause"]}]},
         "not converted"),
        ({"rules": [{"name": "a", "when": "spend > 1", "actions": [{"notify": "tg"}]}]},
         "unknown notifier"),
        ({"rules": [{"name": "a", "when": "spend > 1",
                     "actions": [{"scale_budget": {"percent": 80, "max": 1}}]}]},
         "max_budget_increase_pct"),
        ({"rules": [{"name": "a", "when": "spend > 1",
                     "actions": [{"scale_budget": {"percent": -70}}]}]},
         "max_budget_decrease_pct"),
        ({"notifiers": {"x": {"type": "pigeon"}}}, "type must be"),
        ({"notifiers": {"x": {"type": "telegram", "chat_id": "1"}}}, "bot_token_env"),
        ({"notifiers": {"x": {"type": "webhook"}}}, "url_env"),
        ({"notifiers": {"x": {"type": "email", "host": "h", "from": "a@b"}}}, "to is required"),
        ({"notifiers": {"x": {"type": "email", "host": "h", "from": "a", "to": "b",
                              "security": "tls"}}}, "security"),
        ({"notifiers": {"x": {"type": "telegram", "bot_token_env": "T", "chat_id": 1,
                              "token": "x"}}}, "unknown keys"),
        ({"vars": {"target": "high"}}, "must be a number"),
        ({"account": {"id": "act_1", "currency": "taka"}}, "3-letter"),
        ({"account": {"id": "act_1", "timezone": "Dhaka"}}, "time zone"),
        ({"account": {"id": "nope"}}, "invalid ad account id"),
        ({"api": []}, "must be a mapping"),
    ],
)
def test_config_errors(patch, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_config(minimal(**patch), env={})


def test_not_a_mapping():
    with pytest.raises(ConfigError):
        parse_config(["x"], env={})


def test_warnings():
    cfg = parse_config(minimal(
        rules=[
            {"name": "s", "when": "roas > 3", "cooldown": "30m",
             "actions": [{"scale_budget": 10}]},
            {"name": "n", "when": "spend > 1", "actions": ["notify"]},
            {"name": "z", "when": "spend > 1", "cooldown": 0, "actions": ["pause"]},
        ],
        notifiers={"hook": {"type": "webhook", "url": "https://example.com/hook"}},
    ), env={})
    text = "\n".join(cfg.warnings)
    assert "4 budget changes" in text
    assert "without a max cap" in text
    assert "prefer url_env" in text
    assert "no cooldown" in text


def test_notify_without_notifiers_warns():
    cfg = parse_config(minimal(rules=[{"name": "n", "when": "spend > 1",
                                        "actions": ["notify"]}]), env={})
    assert any("no notifiers" in w for w in cfg.warnings)


def test_load_config_file(tmp_path):
    path = tmp_path / "guard.yaml"
    path.write_text(textwrap.dedent("""
        account:
          id: "act_555"
          currency: BDT
        audit:
          path: logs/audit.jsonl
        rules:
          - name: kill
            when: spend > 1500 BDT and purchases == 0 in last 3 days
            actions: [pause, notify]
    """), encoding="utf-8")
    cfg = load_config(str(path), env={})
    assert cfg.source == path
    assert cfg.audit_path == str(tmp_path.resolve() / "logs" / "audit.jsonl")
    assert cfg.rules[0].window == "3d"


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(str(tmp_path / "missing.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(str(bad))


def test_build_notifiers():
    cfg = parse_config(minimal(notifiers={
        "tg": {"type": "telegram", "bot_token_env": "TG", "chat_id": -100123},
        "slack": {"type": "webhook", "url_env": "HOOK", "field": "content"},
        "mail": {"type": "email", "host": "smtp.example.com", "port": 465, "security": "ssl",
                 "username_env": "SMTP_USER", "password_env": "SMTP_PASS",
                 "from": "alerts@example.com", "to": "owner@example.com"},
    }), env={})
    env = {"TG": "123:abc", "HOOK": "https://hooks.example.com/x", "SMTP_USER": "u",
           "SMTP_PASS": "p"}
    built = build_notifiers(cfg, env=env)
    assert isinstance(built["tg"], TelegramNotifier) and built["tg"].chat_id == "-100123"
    assert isinstance(built["slack"], WebhookNotifier) and built["slack"].field == "content"
    mail = built["mail"]
    assert isinstance(mail, EmailNotifier)
    assert mail.recipients == ["owner@example.com"] and mail.security == "ssl"
    with pytest.raises(ConfigError, match="TG"):
        build_notifiers(cfg, env={})


def test_example_config_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "examples" / "guard.example.yaml"
    cfg = load_config(str(example), env={})
    assert len(cfg.rules) == 6
    assert set(cfg.notifiers) == {"telegram", "slack", "email"}
    assert cfg.rule("stop-spend-without-purchases").window == "3d"
    assert cfg.rule("messenger-leads-too-expensive").result[0].startswith("onsite_conversion")
    assert cfg.warnings == []


def test_offline_demo_runs(capsys):
    import runpy
    from pathlib import Path

    demo = Path(__file__).resolve().parent.parent / "examples" / "offline_demo.py"
    runpy.run_path(str(demo), run_name="__main__")
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "summary: planned 3" in out
