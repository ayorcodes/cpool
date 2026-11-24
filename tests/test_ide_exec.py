import json
import os
import stat
import subprocess
import sys
import time

import pytest

from cpool import ide_exec, paths, usage


class Execed(Exception):
    pass


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("CPOOL_", "ANTHROPIC_")) or key == "CLAUDE_CONFIG_DIR":
            monkeypatch.delenv(key)
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(usage, "has_subscription", lambda acct: None)


@pytest.fixture
def execs(monkeypatch):
    calls = []

    def fake(file, args, env):
        calls.append((file, args, env))
        raise Execed

    monkeypatch.setattr(os, "execvpe", fake)
    return calls


@pytest.fixture
def kicks(monkeypatch):
    calls = []
    monkeypatch.setattr(usage, "kick", lambda cfg, now, *, wait_s=0: calls.append(wait_s) or True)
    return calls


def _pool(tmp_path, extra="", dirs=("a", "b")):
    home = tmp_path / "home"
    body = extra
    for order, name in enumerate(dirs, 1):
        d = "default" if name == "main" else str(tmp_path / name)
        body += f'[[account]]\nname = "{name}"\ndir = "{d}"\norder = {order}\n'
    (home / "config.toml").write_text(body)


def _state(tmp_path, a=5, b=20, updated_at=None, a_extra=None, **both):
    now = int(time.time())
    ts = now if updated_at is None else updated_at
    win = lambda u: {"used": u, "resets_at": now + 3600}  # noqa: E731
    acct = lambda u, **kw: {"five_hour": win(u), "seven_day": win(5), "credits_enabled": False,  # noqa: E731
                            "updated_at": ts, **both, **kw}
    (tmp_path / "home" / "state.json").write_text(json.dumps({"version": 1, "sessions": {}, "accounts": {
        "a": acct(a, **(a_extra or {})), "b": acct(b)}}))


def _exec_of(execs, argv):
    with pytest.raises(Execed):
        ide_exec.main(argv)
    assert len(execs) == 1
    return execs[0]


@pytest.mark.parametrize("args,expected", [
    (["--version"], True), (["-v"], True), (["-h"], True), (["--help"], True),
    (["plugin", "list"], True), (["mcp", "list"], True), (["doctor"], True),
    (["auth", "status"], True), (["update"], True), (["setup-token"], True),
    (["config", "list"], True), (["--model", "opus", "doctor"], True),
    (["--add-dir", "x", "y", "update"], False),
    ([], False),
    (["--output-format", "stream-json", "--input-format", "stream-json"], False),
    (["--resume", "abc"], False),
    (["--resume", "update"], False),
    (["-p", "update"], False),
    (["--system-prompt", "--version"], False),
    (["--", "--version"], False),
    (["--", "update"], False),
    (["hello", "update"], False),
])
def test_is_passthrough(args, expected):
    assert ide_exec.is_passthrough(args) is expected


@pytest.mark.parametrize("args,expected", [
    ([], None),
    (["--model", "opus"], "opus"),
    (["--model=sonnet"], "sonnet"),
    (["--model", "opus", "--model=haiku"], "haiku"),
    (["--system-prompt", "--model", "opus"], None),
    (["--model", "opus", "--", "--model", "haiku"], "opus"),
    (["--", "--model", "haiku"], None),
    (["--model"], None),
])
def test_extract_model(args, expected):
    assert ide_exec.extract_model(args) == expected


def test_no_args_prints_usage(capsys):
    assert ide_exec.main([]) == 2
    assert "usage: cpool-ide-exec" in capsys.readouterr().err


def test_passthrough_execs_untouched_without_config(tmp_path, monkeypatch, execs, kicks):
    (tmp_path / "home" / "config.toml").write_text("not [valid")
    monkeypatch.setenv("CPOOL_RUN", "keep")
    file, args, env = _exec_of(execs, ["/x/claude", "plugin", "list"])
    assert (file, args) == ("/x/claude", ["/x/claude", "plugin", "list"])
    assert env == dict(os.environ)
    assert kicks == []


def test_session_env_contract(tmp_path, monkeypatch, execs, kicks):
    _pool(tmp_path, extra="[shard]\nenabled = true\n")
    _state(tmp_path)
    monkeypatch.setenv("CPOOL_RUN", "r")
    monkeypatch.setenv("CPOOL_RESUMED", "1")
    monkeypatch.setenv("CPOOL_SHARD", "1")
    monkeypatch.setenv("CPOOL_STATUSLINE_NEXT", "x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://parent")
    argv = ["/x/claude", "--output-format", "stream-json", "--resume", "s1"]
    file, args, env = _exec_of(execs, argv)
    assert (file, args) == ("/x/claude", argv)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "a")
    assert env["CPOOL_ACCOUNT"] == "a"
    assert env["CPOOL_IDE"] == "1"
    assert not {"CPOOL_RUN", "CPOOL_RESUMED", "CPOOL_SHARD", "CPOOL_STATUSLINE_NEXT"} & env.keys()
    assert env["ANTHROPIC_BASE_URL"] == "http://parent"
    assert kicks == []
    rec = [json.loads(line) for f in paths.log_dir().glob("*.jsonl") for line in f.read_text().splitlines()]
    assert [(r["kind"], r["account"], r["ide"]) for r in rec] == [("route", "a", True)]


def test_default_account_unsets_config_dir(tmp_path, monkeypatch, execs):
    _pool(tmp_path, dirs=("main",))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/parent/dir")
    now = int(time.time())
    (tmp_path / "home" / "state.json").write_text(json.dumps({"version": 1, "sessions": {}, "accounts": {
        "main": {"five_hour": {"used": 5, "resets_at": now + 3600}, "credits_enabled": False,
                 "updated_at": now}}}))
    _, _, env = _exec_of(execs, ["/x/claude"])
    assert "CLAUDE_CONFIG_DIR" not in env
    assert env["CPOOL_ACCOUNT"] == "main"


@pytest.mark.parametrize("args,expected", [
    (["--model", "opus"], "b"),
    (["--model=opus"], "b"),
    (["--model", "opus", "--model", "sonnet"], "a"),
    (["--", "--model", "opus"], "a"),
    ([], "a"),
])
def test_model_steers_the_pick(tmp_path, execs, args, expected):
    _pool(tmp_path)
    _state(tmp_path, a_extra={"limited": {"model": "opus", "until": int(time.time()) + 3600}})
    argv = ["/x/claude", *args]
    file, passed, env = _exec_of(execs, argv)
    assert passed == argv
    assert env["CPOOL_ACCOUNT"] == expected


def test_stale_usage_kicks_with_bounded_wait_then_routes(tmp_path, execs, kicks):
    _pool(tmp_path)
    _state(tmp_path, updated_at=int(time.time()) - 3600)
    _, _, env = _exec_of(execs, ["/x/claude"])
    assert kicks == [1.5]
    assert env["CPOOL_ACCOUNT"] == "a"


def test_failed_kick_routes_on_cached_state(tmp_path, monkeypatch, execs):
    _pool(tmp_path)
    _state(tmp_path, updated_at=int(time.time()) - 3600)
    monkeypatch.setattr(usage, "kick", lambda cfg, now, *, wait_s=0: False)
    _, _, env = _exec_of(execs, ["/x/claude"])
    assert env["CPOOL_ACCOUNT"] == "a"


def test_no_enabled_accounts_execs_claude_directly(tmp_path, monkeypatch, execs, kicks, capsys):
    monkeypatch.setenv("CPOOL_RUN", "keep")
    _, args, env = _exec_of(execs, ["/x/claude", "--resume", "s"])
    assert args == ["/x/claude", "--resume", "s"]
    assert env == dict(os.environ)
    assert kicks == []
    assert "no enabled accounts" in capsys.readouterr().err


def test_over_soft_threshold_uses_least_used_fallback(tmp_path, execs, capsys):
    _pool(tmp_path)
    _state(tmp_path, a=99, b=90)
    _, _, env = _exec_of(execs, ["/x/claude"])
    assert env["CPOOL_ACCOUNT"] == "b"
    assert "no account under thresholds" in capsys.readouterr().err


@pytest.mark.parametrize("both", [{"credits_enabled": True}, {"credits_enabled": None}, {"auth": "expired"}])
def test_unsafe_accounts_refuse_without_exec(tmp_path, execs, both):
    _pool(tmp_path)
    _state(tmp_path, a=99, b=99, **both)
    assert ide_exec.main(["/x/claude"]) == 1
    assert execs == []


def test_subscriptionless_accounts_refuse_without_exec(tmp_path, monkeypatch, execs, capsys):
    _pool(tmp_path)
    _state(tmp_path)
    monkeypatch.setattr(usage, "has_subscription", lambda acct: False)
    assert ide_exec.main(["/x/claude"]) == 1
    assert execs == []
    assert "no subscription" in capsys.readouterr().err


def test_invalid_config_refuses_without_exec(tmp_path, execs, capsys):
    (tmp_path / "home" / "config.toml").write_text("not [valid")
    assert ide_exec.main(["/x/claude"]) == 2
    assert execs == []
    assert "invalid TOML" in capsys.readouterr().err


def test_missing_claude_binary_exits_127(tmp_path, capsys):
    _pool(tmp_path)
    _state(tmp_path)
    assert ide_exec.main([str(tmp_path / "nope")]) == 127
    assert "failed to exec" in capsys.readouterr().err


def test_exec_replaces_the_wrapper_process(tmp_path):
    _pool(tmp_path)
    _state(tmp_path)
    out = tmp_path / "out.json"
    claude = tmp_path / "claude"
    claude.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                      f"json.dump({{'pid': os.getpid(), 'argv': sys.argv[1:], "
                      f"'cfg': os.environ.get('CLAUDE_CONFIG_DIR'), 'ide': os.environ.get('CPOOL_IDE')}}, "
                      f"open({str(out)!r}, 'w'))\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    proc = subprocess.Popen([sys.executable, "-c", "import sys; from cpool import ide_exec; "
                             "sys.exit(ide_exec.main())", str(claude), "--model", "sonnet"],
                            env={**os.environ, "CPOOL_HOME": str(tmp_path / "home")})
    assert proc.wait(timeout=30) == 0
    rec = json.loads(out.read_text())
    assert rec == {"pid": proc.pid, "argv": ["--model", "sonnet"], "cfg": str(tmp_path / "a"), "ide": "1"}
