import json
import os
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cpool import config, run, shard, state, usage

FAKE = """#!{py}
import json, os, signal, sys, time
home = os.environ["CPOOL_HOME"]
rec = os.path.join(home, "calls.jsonl")
with open(rec, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cfg": os.environ.get("CLAUDE_CONFIG_DIR"),
                        "run": os.environ["CPOOL_RUN"],
                        "sigint": int(signal.getsignal(signal.SIGINT) != signal.SIG_IGN),
                        "sigquit": int(signal.getsignal(signal.SIGQUIT) != signal.SIG_IGN),
                        "next": os.environ.get("CPOOL_STATUSLINE_NEXT"),
                        "env": {{k: v for k, v in os.environ.items()
                                 if k.startswith("ANTHROPIC_") or k == "CPOOL_SHARD"}}}}) + "\\n")
n = sum(1 for _ in open(rec))
if n == 1:
    os.makedirs(os.path.join(home, "handoff"), exist_ok=True)
    with open(os.path.join(home, "handoff", os.environ["CPOOL_RUN"] + ".json"), "w") as f:
        json.dump({{"version": 1, "run": os.environ["CPOOL_RUN"], "session_id": "s1", "from": "a",
                   "model": "claude-sonnet-5", "effort": "xhigh", "reason": os.environ.get("FAKE_REASON", "soft"), "compacted": True,
                   "ts": int(time.time())}}, f)
"""


def _setup(tmp_path, monkeypatch, sessions=None, left_at_b=None):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.write_text(FAKE.format(py=sys.executable))
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    for n in "ab":
        (tmp_path / n).mkdir()
    (home / "config.toml").write_text(
        f'[pool]\njitter_s = [0, 0]\n'
        f'[[account]]\nname = "a"\ndir = "{tmp_path / "a"}"\norder = 1\n'
        f'[[account]]\nname = "b"\ndir = "{tmp_path / "b"}"\norder = 2\n')
    now = int(time.time())
    b_extra = {"left_at": now - 30} if left_at_b else {}
    win = lambda u: {"used": u, "resets_at": now + 3600}  # noqa: E731
    (home / "state.json").write_text(json.dumps({"version": 1, "sessions": sessions or {}, "accounts": {
        "a": {"five_hour": win(5), "seven_day": win(5), "credits_enabled": False, "updated_at": now},
        "b": {"five_hour": win(20), "seven_day": win(5), "credits_enabled": False, "updated_at": now,
              **b_extra},
    }}))
    monkeypatch.setenv("CPOOL_HOME", str(home))
    monkeypatch.setenv("CPOOL_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return home


def _calls(home):
    return [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]


@pytest.fixture(autouse=True)
def shard_boundary(monkeypatch):
    monkeypatch.setattr(usage, "has_subscription", lambda acct: None)
    for key in tuple(os.environ):
        if key.startswith("ANTHROPIC_") or key == "CPOOL_SHARD":
            monkeypatch.delenv(key)
    load = config.load
    mode = SimpleNamespace(enabled=False, url="http://127.0.0.1:8317")

    def load_config():
        cfg = load()
        return replace(cfg, shard=config.Shard(enabled=mode.enabled, url=mode.url))

    monkeypatch.setattr(config, "load", load_config)
    monkeypatch.setattr(shard, "key", lambda: pytest.fail("disabled shard read key"))
    monkeypatch.setattr(shard, "probe", lambda *a, **kw: pytest.fail("disabled shard probed"))
    monkeypatch.setattr(shard, "relay_supports_shard", lambda url: False)
    return mode, shard


@pytest.mark.parametrize("sharded", [False, True])
def test_launch_skips_subscriptionless_cached_ok_account(tmp_path, monkeypatch, shard_boundary, sharded):
    home = _setup(tmp_path, monkeypatch)
    mode, proxy = shard_boundary
    mode.enabled = sharded
    monkeypatch.setattr(proxy, "key", lambda: "test-key")
    monkeypatch.setattr(proxy, "probe", lambda *a, **kw: False)
    monkeypatch.setattr(usage, "has_subscription", lambda acct: acct.name != "a")
    with state.locked() as st:
        st["accounts"]["a"]["auth"] = "ok"
    assert run.main(["-p", "--", "hi"]) == 0
    assert [call["cfg"] for call in _calls(home)] == [str(tmp_path / "b")]


def test_all_subscriptionless_accounts_do_not_launch(tmp_path, monkeypatch, capsys):
    home = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(usage, "has_subscription", lambda acct: False)
    assert run.main(["--", "hi"]) == 1
    assert not (home / "calls.jsonl").exists()
    assert "no subscription: a, b" in capsys.readouterr().err


@pytest.mark.parametrize("auth", ["no-plan", "missing", "expired"])
def test_pinned_account_rejects_cached_auth(tmp_path, monkeypatch, capsys, auth):
    home = _setup(tmp_path, monkeypatch)
    with state.locked() as st:
        st["accounts"]["a"]["auth"] = auth
    assert run.main(["--account", "a", "--", "hi"]) == 1
    assert not (home / "calls.jsonl").exists()
    assert f"auth: {auth}" in capsys.readouterr().err


def test_pinned_account_rejects_live_no_subscription(tmp_path, monkeypatch, capsys):
    home = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(usage, "has_subscription", lambda acct: acct.name != "a")
    assert run.main(["--account", "a", "--", "hi"]) == 1
    assert not (home / "calls.jsonl").exists()
    assert "account 'a' has no subscription" in capsys.readouterr().err


def test_expired_account_is_not_threshold_fallback(tmp_path, monkeypatch):
    home = _setup(tmp_path, monkeypatch)
    with state.locked() as st:
        for acct in st["accounts"].values():
            acct["auth"] = "expired"
    assert run.main(["--", "hi"]) == 1
    assert not (home / "calls.jsonl").exists()


def test_handoff_rechecks_subscription(tmp_path, monkeypatch, capsys):
    home = _setup(tmp_path, monkeypatch)
    membership = {"a": True, "b": True}
    monkeypatch.setattr(usage, "has_subscription", lambda acct: membership[acct.name])
    spawn = run._spawn

    def launch(cmd, env):
        code = spawn(cmd, env)
        membership["b"] = False
        return code

    monkeypatch.setattr(run, "_spawn", launch)
    assert run.main(["--", "hi"]) == 0
    assert len(_calls(home)) == 1
    assert "b: no subscription" in capsys.readouterr().err


def test_subscription_removed_after_routing_does_not_launch(tmp_path, monkeypatch, capsys):
    home = _setup(tmp_path, monkeypatch)
    checked = set()

    def membership(acct):
        if acct.name in checked:
            return False
        checked.add(acct.name)
        return True

    monkeypatch.setattr(usage, "has_subscription", membership)
    assert run.main(["--", "hi"]) == 1
    assert not (home / "calls.jsonl").exists()
    assert "account 'a' has no subscription" in capsys.readouterr().err


@pytest.mark.parametrize("interval", [15, 0])
def test_statusline_refresh_setting(tmp_path, monkeypatch, interval):
    home = _setup(tmp_path, monkeypatch)
    path = home / "config.toml"
    path.write_text(path.read_text().replace("[pool]", f"[pool]\nstatusline_refresh_s = {interval}"))
    observed = []
    def spawn(cmd, env):
        settings = json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text())
        observed.append(settings["statusLine"])
        return 0
    monkeypatch.setattr(run, "_spawn", spawn)
    assert run.main(["--", "hi"]) == 0
    assert observed[0].get("refreshInterval") == (interval or None)
    assert ("refreshInterval" in observed[0]) is bool(interval)


def test_handoff_loop_relaunches_on_other_account(tmp_path, monkeypatch, shard_boundary):
    home = _setup(tmp_path, monkeypatch)
    for key in tuple(os.environ):
        if key.startswith("ANTHROPIC_") or key == "CPOOL_SHARD":
            monkeypatch.delenv(key)

    assert run.main(["--", "original prompt"]) == 0

    calls = _calls(home)
    assert len(calls) == 2
    first, second = calls
    assert first["env"] == second["env"] == {}
    assert first["cfg"] == str(tmp_path / "a") and second["cfg"] == str(tmp_path / "b")
    assert first["run"] == second["run"]
    assert second["argv"].count("--resume") == 1
    assert "--effort" not in first["argv"]
    assert second["argv"].count("--effort") == 1
    assert second["argv"][second["argv"].index("--effort") + 1] == "xhigh"
    assert second["argv"][second["argv"].index("--resume") + 1] == "s1"
    assert "original prompt" in first["argv"] and "original prompt" not in second["argv"]
    assert second["argv"].count("--plugin-dir") == 1 and second["argv"].count("--settings") == 1
    assert state.read()["accounts"]["a"]["left_at"] is not None
    assert not list((home / "run").glob("*.settings.json"))
    assert first["sigint"] == 1 and first["sigquit"] == 1


@pytest.mark.parametrize("pending_handoff", [True, False])
@pytest.mark.parametrize("args", [["--", "hi"], ["-p", "--", "hi"]])
def test_shard_routes_without_account_switching(
    tmp_path, monkeypatch, capsys, shard_boundary, pending_handoff, args
):
    home = _setup(tmp_path, monkeypatch)
    mode, proxy = shard_boundary
    mode.enabled = True
    probes = []
    monkeypatch.setattr(proxy, "key", lambda: "test-proxy-key")

    def probe(url, key, timeout=2.0):
        probes.append((url, key, timeout))
        return True

    monkeypatch.setattr(proxy, "probe", probe)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-api-key")
    monkeypatch.setattr(run, "_hard_handoff", lambda *a: pytest.fail("shard prompted for handoff"))
    claude = tmp_path / "bin" / "claude"
    script = FAKE.format(py=sys.executable)
    if not pending_handoff:
        script = script.replace("if n == 1:", "if False:")
    claude.write_text(script + "\nsys.exit(7)\n")

    assert run.main(args) == 7

    calls = _calls(home)
    assert len(calls) == 1
    assert calls[0]["cfg"] == str(tmp_path / "a")
    assert calls[0]["env"] == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
        "ANTHROPIC_AUTH_TOKEN": "test-proxy-key",
        "CPOOL_SHARD": "1",
    }
    assert probes == [("http://127.0.0.1:8317", "test-proxy-key", 2.0)]
    assert not list((home / "handoff").glob("*.json"))
    assert "left_at" not in state.read()["accounts"]["a"]
    err = capsys.readouterr().err
    assert "http://127.0.0.1:9999" in err and "overrid" in err
    assert "test-proxy-key" not in err and "old-token" not in err
    assert os.environ["ANTHROPIC_API_KEY"] == "old-api-key"
    assert not list((home / "run").glob("*.settings.json"))


@pytest.mark.parametrize("headers,expected", [
    (None, "x-cpool-shard: 1"),
    ("x-custom: keep", "x-custom: keep\nx-cpool-shard: 1"),
    ("x-custom: keep\nx-cpool-shard: 1", "x-custom: keep\nx-cpool-shard: 1"),
    ("X-Cpool-Shard: 1", "X-Cpool-Shard: 1"),
])
def test_shard_preserves_relay_and_merges_headers(
    tmp_path, monkeypatch, capsys, shard_boundary, headers, expected
):
    home = _setup(tmp_path, monkeypatch)
    mode, proxy = shard_boundary
    mode.enabled = True
    monkeypatch.setattr(proxy, "key", lambda: "test-proxy-key")
    monkeypatch.setattr(proxy, "probe", lambda *a, **kw: True)
    monkeypatch.setattr(proxy, "relay_supports_shard", lambda url: url == "http://localhost:4001")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4001")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-api-key")
    if headers is None:
        monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", headers)

    assert run.main(["--", "hi"]) == 0

    assert _calls(home)[0]["env"] == {
        "ANTHROPIC_BASE_URL": "http://localhost:4001",
        "ANTHROPIC_AUTH_TOKEN": "test-proxy-key",
        "ANTHROPIC_CUSTOM_HEADERS": expected,
        "CPOOL_SHARD": "1",
    }
    assert capsys.readouterr().err == ""


def test_shard_without_parent_base_routes_direct(tmp_path, monkeypatch, capsys, shard_boundary):
    home = _setup(tmp_path, monkeypatch)
    mode, proxy = shard_boundary
    mode.enabled = True
    monkeypatch.setattr(proxy, "key", lambda: "test-proxy-key")
    monkeypatch.setattr(proxy, "probe", lambda *a, **kw: True)
    monkeypatch.setattr(proxy, "relay_supports_shard", lambda url: pytest.fail("no parent to probe"))
    for name in tuple(os.environ):
        if name.startswith("ANTHROPIC_") or name == "CPOOL_SHARD":
            monkeypatch.delenv(name)

    assert run.main(["--", "hi"]) == 0

    assert _calls(home)[0]["env"] == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
        "ANTHROPIC_AUTH_TOKEN": "test-proxy-key",
        "CPOOL_SHARD": "1",
    }
    assert capsys.readouterr().err == ""


def test_disabled_shard_preserves_parent_environment(tmp_path, monkeypatch, shard_boundary):
    home = _setup(tmp_path, monkeypatch)
    _, proxy = shard_boundary
    monkeypatch.setattr(proxy, "relay_supports_shard", lambda url: pytest.fail("disabled relay probe"))
    for name in tuple(os.environ):
        if name.startswith("ANTHROPIC_") or name == "CPOOL_SHARD":
            monkeypatch.delenv(name)
    expected = {
        "ANTHROPIC_BASE_URL": "http://localhost:4001",
        "ANTHROPIC_AUTH_TOKEN": "parent-token",
        "ANTHROPIC_API_KEY": "parent-api-key",
        "ANTHROPIC_CUSTOM_HEADERS": "x-custom: keep",
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)

    assert run.main(["--", "hi"]) == 0

    assert all(call["env"] == expected for call in _calls(home))


@pytest.mark.parametrize("key", ["test-proxy-key", None])
def test_shard_unavailable_falls_back(tmp_path, monkeypatch, capsys, shard_boundary, key):
    home = _setup(tmp_path, monkeypatch)
    mode, proxy = shard_boundary
    mode.enabled = True
    monkeypatch.setattr(proxy, "key", lambda: key)
    probes = []

    def probe(url, token, timeout=2.0):
        probes.append((url, token, timeout))
        return False

    monkeypatch.setattr(proxy, "probe", probe)
    monkeypatch.delenv("CPOOL_SHARD", raising=False)
    for name in tuple(os.environ):
        if name.startswith("ANTHROPIC_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "relay-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "relay-key")

    assert run.main(["--", "hi"]) == 0

    calls = _calls(home)
    assert [call["cfg"] for call in calls] == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert all(call["env"] == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
        "ANTHROPIC_AUTH_TOKEN": "relay-token",
        "ANTHROPIC_API_KEY": "relay-key",
    } for call in calls)
    assert probes == ([] if key is None else [("http://127.0.0.1:8317", "test-proxy-key", 2.0)])
    err = capsys.readouterr().err
    assert "cpool: shard proxy unreachable or key missing " \
           "(set CPOOL_SHARD_KEY or CLI_PROXY_API_KEY), running unsharded" in err
    assert "test-proxy-key" not in err and "relay-token" not in err
    assert not list((home / "handoff").glob("*.json"))


def test_print_mode_does_not_relaunch(tmp_path, monkeypatch, capsys):
    home = _setup(tmp_path, monkeypatch)

    assert run.main(["-p", "--", "hi"]) == 0

    assert len(_calls(home)) == 1
    assert "a over threshold; next run will route elsewhere" in capsys.readouterr().err
    assert state.read()["accounts"]["a"]["left_at"] is not None
    assert not list((home / "handoff").glob("*.json"))


def test_handoff_clears_leave_flags(tmp_path, monkeypatch):
    sess = {"s1": {"pid": os.getpid(), "account": "a", "leave": True, "leave_manual": True}}
    home = _setup(tmp_path, monkeypatch, sessions=sess)

    assert run.main(["--", "hi"]) == 0

    assert len(_calls(home)) == 2
    s1 = state.read()["sessions"]["s1"]
    assert not s1["leave"] and not s1["leave_manual"]


def test_statusline_falls_back_to_account_settings(tmp_path, monkeypatch):
    home = _setup(tmp_path, monkeypatch)
    (tmp_path / "a" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "my-line"}}))
    (tmp_path / "user.json").write_text("{}")

    assert run.main(["--settings", str(tmp_path / "user.json"), "--", "hi"]) == 0

    first, second = _calls(home)
    assert first["next"] == "my-line" and second["next"] is None


@pytest.mark.parametrize("args,expected", [
    (["--resume", "s", "hi"], []),
    (["-c", "--model", "m", "do it"], ["--model", "m"]),
    (["--session-id", "x", "--fork-session", "--verbose"], ["--verbose"]),
    (["--add-dir", "d1", "d2", "--name", "n", "prompt"], ["--add-dir", "d1", "d2", "--name", "n"]),
    (["--resume=s", "--", "-p", "x"], []),
    (["--worktree", "w1", "hi"], ["--worktree", "w1"]),
    (["-w", "--verbose"], ["-w", "--verbose"]),
    (["--prompt-suggestions", "on", "hi"], ["--prompt-suggestions", "on"]),
])
def test_relaunch_args(args, expected):
    assert run.relaunch_args(args) == expected


def test_manual_handoff_ignores_cooldown(tmp_path, monkeypatch, capsys):
    home = _setup(tmp_path, monkeypatch, left_at_b=True)
    monkeypatch.setenv("FAKE_REASON", "manual")

    assert run.main(["--", "hi"]) == 0

    assert [c["cfg"] for c in _calls(home)] == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_automatic_handoff_respects_cooldown_and_says_why(tmp_path, monkeypatch, capsys):
    home = _setup(tmp_path, monkeypatch, left_at_b=True)

    assert run.main(["--", "hi"]) == 0

    assert len(_calls(home)) == 1
    err = capsys.readouterr().err
    assert "b: cooldown until" in err and "a: current" in err and "earliest reset" not in err
