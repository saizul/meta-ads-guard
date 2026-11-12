import io
import json
import textwrap

import pytest

from meta_ads_guard.cli import main

from .conftest import TOKEN, graph_error, respond
from .fakeaccount import FakeAccount

CONFIG = """
account:
  id: act_1234567890
  currency: BDT
  timezone: Asia/Dhaka
api:
  version: v24.0
state:
  path: state.sqlite
audit:
  path: audit.jsonl
notifiers:
  slack:
    type: webhook
    url_env: SLACK_WEBHOOK_URL
rules:
  - name: kill-no-purchase
    when: spend > 1500 BDT and purchases == 0 in last 3 days
    actions: [pause, notify]
    cooldown: 12h
  - name: fatigue
    level: ad
    when: frequency[7d] > 3.5
    actions: [notify]
"""

ENV = {"META_ACCESS_TOKEN": TOKEN, "SLACK_WEBHOOK_URL": "https://hooks.example.com/services/x"}


@pytest.fixture
def cfg_path(tmp_path):
    path = tmp_path / "guard.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    return path


@pytest.fixture
def account(transport):
    acc = FakeAccount(transport)
    acc.add_adset("101", "Cold Video")
    acc.add_adset("102", "Retargeting")
    acc.add_ad("201", "Ad A")
    acc.insights("adset", "*", "101", spend=1800, impressions=20000, clicks=200, reach=9000)
    acc.insights("adset", "*", "102", spend=900, impressions=9000, clicks=90, reach=5000,
                 actions={"purchase": 3})
    acc.insights("ad", "*", "201", impressions=5000, reach=1000, frequency=5)
    transport.add("POST", r"/services/x$", respond(None, 200))
    return acc


def run(argv, transport=None, env=ENV):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, env=env, transport=transport, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_validate(cfg_path):
    code, out, _ = run(["validate", "-c", str(cfg_path)], env={})
    assert code == 0
    assert "config OK: 2 rule(s)" in out
    assert "kill-no-purchase" in out and "slack (webhook)" in out


def test_validate_reports_config_errors(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("account: {id: act_1}\nrules: [{name: x, when: 'spendd > 1', actions: "
                    "[pause]}]\n")
    code, _, err = run(["validate", "-c", str(path)], env={})
    assert code == 2
    assert "did you mean 'spend'" in err


def test_run_dry_run_text(cfg_path, transport, account):
    code, out, _ = run(["run", "-c", str(cfg_path)], transport)
    assert code == 0
    assert "DRY RUN" in out
    assert "kill-no-purchase" in out and "Cold Video" in out and "planned" in out
    assert "fatigue" in out
    assert account.mutations == []
    assert not transport.find("POST", r"/services/x$")
    audit = (cfg_path.parent / "audit.jsonl").read_text().splitlines()
    assert all(json.loads(line)["dry_run"] for line in audit)


def test_run_apply_json(cfg_path, transport, account):
    code, out, _ = run(["run", "-c", str(cfg_path), "--apply", "--json"], transport)
    assert code == 0
    report = json.loads(out)
    assert report["dry_run"] is False
    assert report["counts"]["applied"] == 1
    assert account.mutations[0][0] == "101"
    hook = transport.find("POST", r"/services/x$")
    assert len(hook) == 1 and "Cold Video" in hook[0].json_body["text"]
    # second run: cooldown for the paused ad set is stored in SQLite
    code, out, _ = run(["run", "-c", str(cfg_path), "--apply", "--json", "--rule",
                        "kill-no-purchase"], transport)
    assert json.loads(out)["matches"][0]["status"] == "cooldown"
    code, out, _ = run(["cooldowns", "-c", str(cfg_path)])
    assert "kill-no-purchase" in out and "101" in out
    code, out, _ = run(["cooldowns", "-c", str(cfg_path), "--clear", "--rule", "fatigue"])
    assert "cleared 1" in out
    code, out, _ = run(["audit", "-c", str(cfg_path), "--tail", "50"])
    assert "pause" in out and "applied" in out
    code, out, _ = run(["audit", "-c", str(cfg_path), "--json"])
    assert isinstance(json.loads(out), list)


def test_run_with_rule_filter_and_notify_flag(cfg_path, transport, account):
    code, _, _ = run(["run", "-c", str(cfg_path), "--rule", "fatigue", "--notify"], transport)
    assert code == 0
    hook = transport.find("POST", r"/services/x$")
    assert hook and hook[0].json_body["text"].startswith("*[DRY RUN]")


def test_missing_token(cfg_path):
    code, _, err = run(["run", "-c", str(cfg_path)], env={})
    assert code == 2 and "META_ACCESS_TOKEN" in err


def test_auth_error_exit_code(cfg_path, transport):
    transport.add("GET", r"/act_1234567890$", graph_error(190, "Error validating access token"))
    code, _, err = run(["run", "-c", str(cfg_path)], transport)
    assert code == 3 and "authentication error" in err


def test_api_error_exit_code(cfg_path, transport):
    transport.add("GET", r"/act_1234567890$", graph_error(100, "Unsupported get request"))
    code, _, err = run(["run", "-c", str(cfg_path)], transport)
    assert code == 1 and "Unsupported" in err


def test_failed_action_gives_exit_1(cfg_path, transport, account):
    account.fail_mutation = graph_error(100, "Invalid parameter")
    code, out, _ = run(["run", "-c", str(cfg_path), "--apply"], transport)
    assert code == 1 and "errors:" in out


def test_insights_command(cfg_path, transport, account):
    code, out, _ = run(["insights", "-c", str(cfg_path), "--level", "adset", "--window", "3d"],
                       transport)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("adset insights, 3d")
    assert lines[3].startswith("101")  # sorted by spend, descending
    code, out, _ = run(["insights", "-c", str(cfg_path), "--json", "--sort", "purchases"],
                       transport)
    data = json.loads(out)
    assert data["rows"][0]["id"] == "102"


def test_metrics_command():
    code, out, _ = run(["metrics"], env={})
    assert code == 0 and "cost_per_result" in out and "omni_purchase" in out


def test_audit_without_path(tmp_path):
    path = tmp_path / "g.yaml"
    path.write_text("account: {id: act_1}\nrules: [{name: x, when: 'spend > 1', "
                    "actions: [pause]}]\n")
    code, _, err = run(["audit", "-c", str(path)], env={})
    assert code == 2 and "no audit.path" in err


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "meta-ads-guard 0.1.0" in capsys.readouterr().out
