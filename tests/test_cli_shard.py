"""CLI shard routing/auth gates and live counts lack existing entry-point coverage."""
import json
import os

import pytest

import io
import subprocess
from unittest.mock import Mock
from urllib.error import HTTPError

from cpool import cli, config, shard, shardacct


def test_shard_on_off_preserves_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CPOOL_SHARD_KEY", "secret")
    config.save_shard(config.Shard(url="http://localhost:9000"))
    monkeypatch.setattr(shard, "probe", lambda url, key: True)
    assert cli.main(["shard", "on"]) == 0
    assert config.load().shard == config.Shard(True, "http://localhost:9000")
    assert capsys.readouterr().out == (
        "shard on: new cpool run sessions route via http://localhost:9000; running sessions unchanged\n"
    )

    def no_probe(*args):
        pytest.fail("off must not probe")

    monkeypatch.setattr(shard, "probe", no_probe)
    assert cli.main(["shard", "off"]) == 0
    assert config.load().shard == config.Shard(False, "http://localhost:9000")


@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("exists", [True, False])
def test_shard_on_failure_never_writes(tmp_path, monkeypatch, capsys, present, exists):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.delenv("CLI_PROXY_API_KEY", raising=False)
    monkeypatch.delenv("CPOOL_SHARD_KEY", raising=False)
    if present:
        monkeypatch.setenv("CLI_PROXY_API_KEY", "secret")
    path = tmp_path / "config.toml"
    original = '[shard]\nenabled = false\nurl = "http://localhost:9000"\n'
    if exists:
        path.write_text(original)
    monkeypatch.setattr(shard, "probe", lambda url, key: False)
    assert cli.main(["shard", "on"]) == 1
    error = capsys.readouterr().err
    if present:
        assert "unreachable" in error
    else:
        assert "CPOOL_SHARD_KEY" in error and "CLI_PROXY_API_KEY" in error
    assert "secret" not in error
    if exists:
        assert path.read_text() == original
    else:
        assert not path.exists()


@pytest.mark.parametrize("relay", [True, False])
@pytest.mark.parametrize("enabled,reachable,present", [(True, True, True), (False, False, False)])
def test_shard_status_counts_live_shard_sessions(tmp_path, monkeypatch, capsys, enabled, reachable, present, relay):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://relay:8080")
    monkeypatch.delenv("CPOOL_SHARD_KEY", raising=False)
    monkeypatch.delenv("CLI_PROXY_API_KEY", raising=False)
    if present:
        monkeypatch.setenv("CPOOL_SHARD_KEY", "secret")
    config.save_shard(config.Shard(enabled))
    (tmp_path / "state.json").write_text(json.dumps({"sessions": {
        "live": {"shard": True, "pid": os.getpid()},
        "dead": {"shard": True, "pid": 999999},
        "normal": {"pid": os.getpid()},
    }}))
    monkeypatch.setattr(shard, "probe", lambda url, key: reachable)
    monkeypatch.setattr(shard, "relay_supports_shard", lambda url: relay and url == "http://relay:8080")
    assert cli.main(["shard", "status"]) == 0
    output = capsys.readouterr().out
    assert f"shard: {'enabled' if enabled else 'disabled'}" in output
    assert "url: http://127.0.0.1:8317" in output
    assert f"reachable: {'yes' if reachable else 'no'}" in output
    assert f"key: {'present' if present else 'absent'}" in output
    assert "live shard sessions: 1" in output
    assert ("relay/lite route is overridden by shard" in output) is (not relay)
    route = "via relay http://relay:8080" if relay else "direct http://127.0.0.1:8317"
    assert f"route: {route}\n" in output
    assert "secret" not in output


@pytest.mark.parametrize("strategy,affinity,key", [("fill-first", True, "key"), ("round-robin", True, "key"), ("fill-first", False, "key"), ("fill-first", True, None)])
@pytest.mark.parametrize("enabled,reachable", [(False, False), (True, False), (True, True)])
def test_doctor_reports_shard_only_when_enabled(tmp_path, monkeypatch, capsys, enabled, reachable, strategy, affinity, key):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    config.save_shard(config.Shard(enabled))
    monkeypatch.setattr(shard, "probe", lambda url, key: reachable)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in (cli.launchd.WARM, cli.launchd.POLL):
        (agents / f"{label}.plist").touch()
    monkeypatch.setattr(cli.launchd, "health", lambda label: None)
    monkeypatch.setattr(shardacct, "get_key", lambda: key)
    monkeypatch.setattr(shardacct, "mgmt", lambda *args: {"routing": {"strategy": strategy, "session-affinity": affinity}})
    assert cli.main(["doctor"]) == int(enabled and (not reachable or not key or strategy != "fill-first" or not affinity))
    output = capsys.readouterr().out
    assert ("shard proxy" in output) is enabled
    if enabled and key:
        assert ("✓ " if strategy == "fill-first" else "✗ ") + "cliproxyapi routing.strategy" in output
        assert ("✓ " if affinity else "✗ ") + "cliproxyapi routing.session-affinity" in output
        assert output.count("cpool does not edit it") == int(strategy != "fill-first" or not affinity)
    elif enabled:
        assert "✗ shard management key: absent" in output


@pytest.mark.parametrize("source", ["env", "keychain", "absent"])
def test_management_key_status_and_secret_safe_check(tmp_path, monkeypatch, capsys, source):
    """CLI checks must not leak secrets or silently bypass the rejection latch."""
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setattr(shardacct, "_rejected", set())
    secret = "management-test-secret"
    monkeypatch.delenv("CPOOL_SHARD_MGMT_KEY", raising=False)
    if source == "env":
        monkeypatch.setenv("CPOOL_SHARD_MGMT_KEY", secret)
    run = Mock(return_value=subprocess.CompletedProcess([], int(source == "absent"), secret, ""))
    monkeypatch.setattr(shardacct.subprocess, "run", run)
    http = Mock(side_effect=lambda *a, **kw: io.BytesIO(b'{"files":[{"auth_index":"idx","name":"login"}]}'))
    monkeypatch.setattr(shardacct.urllib.request.OpenerDirector, "open", http)
    assert cli.main(["shard", "mgmt-key", "status", "--check"]) == (2 if source == "absent" else 0)
    output = capsys.readouterr().out
    assert ("absent" if source == "absent" else f"present ({source})") in output
    assert secret not in output
    if source == "absent":
        http.assert_not_called()
        return
    assert "ok: 1 auths" in output
    http.side_effect = HTTPError("http://localhost", 401, "rejected", {}, None)
    assert cli.main(["shard", "mgmt-key", "status", "--check"]) == 2
    assert "401: key rejected (4 more failures lock management for 30 min)" in capsys.readouterr().out
    assert cli.main(["shard", "mgmt-key", "status"]) == 0
    assert "auth" in capsys.readouterr().out
    assert http.call_count == 2
    assert shardacct.auth_emails(9999999999) is None
    assert http.call_count == 2
    assert cli.main(["shard", "mgmt-key", "status", "--check"]) == 2
    output = capsys.readouterr().out
    assert "3 more failures" in output
    assert secret not in output + (tmp_path / "shard-auths.json").read_text()
    assert http.call_count == 3


def test_management_set_and_clear_use_hidden_prompt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    secret = "prompt-management-secret"
    monkeypatch.setattr(shardacct.getpass, "getpass", lambda prompt: secret)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(shardacct.subprocess, "run", run)
    assert cli.main(["shard", "mgmt-key", "set"]) == 0
    assert run.call_args.args[0] == ["security", "-i"]
    assert secret not in str(run.call_args.args)
    assert cli.main(["shard", "mgmt-key", "clear"]) == 0
    assert "key removed" in capsys.readouterr().out


def test_status_outside_shard_shows_serving_rows(tmp_path, monkeypatch, capsys):
    """The detailed status entry point must share the statusline attribution."""
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.delenv("CPOOL_SHARD", raising=False)
    monkeypatch.setattr(shardacct, "_rejected", set())
    monkeypatch.setenv("CPOOL_SHARD_MGMT_KEY", "test-management-secret")
    monkeypatch.setattr(shardacct.subprocess, "run", Mock(side_effect=AssertionError("no Keychain")))
    monkeypatch.setattr(shardacct.urllib.request.OpenerDirector, "open", Mock(side_effect=lambda *a, **kw:
                        io.BytesIO(b'{"files":[{"auth_index":"abc123","email":"serving@example.com"}]}')))
    monkeypatch.setattr(shardacct, "SHARD_AUTH", tmp_path / "relay.json")
    shardacct.SHARD_AUTH.write_text('{"v":1,"sessions":{"s":{"auth_index":"abc123","ts":1}}}')
    account = tmp_path / "serving"
    account.mkdir()
    (account / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"serving@example.com"}}')
    cfg = config.Config(config.Pool(), (config.Account("launch", None, 0), config.Account("serving", account, 1)))
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(cli.state, "read", lambda: {"sessions": {"s": {"shard": True, "pid": os.getpid()}}})
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "⇄ shard 1" in output
    assert "▸ serving  ●" in output
    assert "▸ serving" in output
    assert "▸ launch" not in output
