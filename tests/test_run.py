import json
import os
import stat
import sys
import time

import pytest

from cpool import run, state

FAKE = """#!{py}
import json, os, signal, sys, time
home = os.environ["CPOOL_HOME"]
rec = os.path.join(home, "calls.jsonl")
with open(rec, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cfg": os.environ.get("CLAUDE_CONFIG_DIR"),
                        "run": os.environ["CPOOL_RUN"],
                        "sigint": int(signal.getsignal(signal.SIGINT) != signal.SIG_IGN),
                        "sigquit": int(signal.getsignal(signal.SIGQUIT) != signal.SIG_IGN),
                        "next": os.environ.get("CPOOL_STATUSLINE_NEXT")}}) + "\\n")
n = sum(1 for _ in open(rec))
if n == 1:
    os.makedirs(os.path.join(home, "handoff"), exist_ok=True)
    with open(os.path.join(home, "handoff", os.environ["CPOOL_RUN"] + ".json"), "w") as f:
        json.dump({{"version": 1, "run": os.environ["CPOOL_RUN"], "session_id": "s1", "from": "a",
                   "model": "claude-sonnet-5", "reason": "soft", "compacted": True,
                   "ts": int(time.time())}}, f)
"""


def _setup(tmp_path, monkeypatch, sessions=None):
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
    win = lambda u: {"used": u, "resets_at": now + 3600}  # noqa: E731
    (home / "state.json").write_text(json.dumps({"version": 1, "sessions": sessions or {}, "accounts": {
        "a": {"five_hour": win(5), "seven_day": win(5), "credits_enabled": False, "updated_at": now},
        "b": {"five_hour": win(20), "seven_day": win(5), "credits_enabled": False, "updated_at": now},
    }}))
    monkeypatch.setenv("CPOOL_HOME", str(home))
    monkeypatch.setenv("CPOOL_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return home


def _calls(home):
    return [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]


def test_handoff_loop_relaunches_on_other_account(tmp_path, monkeypatch):
    home = _setup(tmp_path, monkeypatch)

    assert run.main(["--", "original prompt"]) == 0

    calls = _calls(home)
    assert len(calls) == 2
    first, second = calls
    assert first["cfg"] == str(tmp_path / "a") and second["cfg"] == str(tmp_path / "b")
    assert first["run"] == second["run"]
    assert second["argv"].count("--resume") == 1
    assert second["argv"][second["argv"].index("--resume") + 1] == "s1"
    assert "original prompt" in first["argv"] and "original prompt" not in second["argv"]
    assert second["argv"].count("--plugin-dir") == 1 and second["argv"].count("--settings") == 1
    assert state.read()["accounts"]["a"]["left_at"] is not None
    assert not list((home / "run").glob("*.settings.json"))
    assert first["sigint"] == 1 and first["sigquit"] == 1


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
