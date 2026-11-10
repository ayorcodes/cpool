"""status reads the persisted state contract that other processes write."""

import json
import os
import time

import pytest

from cpool import cli


def test_status_json_reads_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[[account]]\nname = "main"\ndir = "default"\norder = 1\n\n'
        '[[account]]\nname = "claude2"\ndir = "~/.claude2"\norder = 2\n'
    )
    now = int(time.time())
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "main": {
                        "five_hour": {"used": 42.0, "resets_at": now + 3600},
                        "seven_day": {"used": 11.0, "resets_at": now + 86400},
                        "credits_enabled": False,
                        "tier": "default_claude_max_5x",
                        "source": "statusline",
                        "updated_at": now - 120,
                        "auth": "ok",
                    },
                    "claude2": {
                        "five_hour": {"used": 80.0, "resets_at": now - 60},
                        "seven_day": {"used": 5.0, "resets_at": now + 86400},
                        "credits_enabled": False,
                        "tier": "default_claude_ai",
                        "source": "poll",
                        "updated_at": now - 600,
                        "auth": "ok",
                    },
                },
                "sessions": {},
            }
        )
    )

    assert cli.main(["status", "--json"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)["accounts"]}

    assert rows["main"]["tier"] == "Max5x"
    assert rows["main"]["five_hour"]["used"] == 42.0
    assert rows["main"]["source"] == "statusline" and rows["main"]["age"] == "2m"
    assert rows["claude2"]["tier"] == "Pro"
    assert rows["claude2"]["five_hour"] == {"used": 0.0, "resets_at": None}
    assert rows["claude2"]["seven_day"]["used"] == 5.0


def test_status_text_lists_accounts_without_ansi_when_piped(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    (tmp_path / "config.toml").write_text(
        '[[account]]\nname = "main"\ndir = "default"\norder = 1\n\n'
        '[[account]]\nname = "other"\ndir = "~/other"\norder = 2\n'
    )
    (tmp_path / "state.json").write_text(json.dumps({"accounts": {
        "main": {"five_hour": {"used": 42, "resets_at": 4600},
                 "seven_day": {"used": 11, "resets_at": 87400},
                 "credits_enabled": False, "auth": "ok", "updated_at": 1000}
    }}))
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert "\x1b" not in output
    assert len(lines) == 3
    assert lines[0].startswith("● main   5h ▰▰▰▱▱▱▱▱  42% ↻1h00m  7d ▰▱▱▱▱▱▱▱  11% ↻1d0h   ?  no data")
    assert lines[1].startswith("○ other  5h ▱▱▱▱▱▱▱▱    –         7d ▱▱▱▱▱▱▱▱    –")
    assert "credits" not in lines[0]
    assert lines[-1] == "→ next: main (best)"


def test_switch_and_status_ignore_dead_sessions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cwd = str(tmp_path.resolve())
    monkeypatch.setattr(cli.os, "getcwd", lambda: cwd)
    (tmp_path / "config.toml").write_text('[[account]]\nname = "main"\ndir = "default"\norder = 1\n')
    now = int(time.time())
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {},
                "sessions": {
                    "live": {"account": "main", "cwd": cwd, "pid": os.getpid(), "updated_at": now - 100},
                    "dead": {"account": "main", "cwd": cwd, "pid": 999999, "updated_at": now},
                },
            }
        )
    )

    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["accounts"][0]["sessions"] == 1

    assert cli.main(["switch", "dead"]) == 2
    assert cli.main(["switch"]) == 0
    sessions = json.loads((tmp_path / "state.json").read_text())["sessions"]
    assert sessions["live"]["leave_manual"] is True
    assert "dead" not in sessions


def test_main_dispatches_bare_and_flag_argv_to_run(monkeypatch):
    from cpool import run

    seen = []
    monkeypatch.setattr(run, "main", lambda argv: seen.append(argv) or 0)

    for argv in ([], ["--model", "opus"], ["--resume", "x"]):
        assert cli.main(argv) == 0
    assert seen == [[], ["--model", "opus"], ["--resume", "x"]]

    seen.clear()
    monkeypatch.setattr(cli, "_cmd_status", lambda as_json: 0)
    assert cli.main(["status", "--json"]) == 0
    try:
        cli.main(["-h"])
    except SystemExit as e:
        assert e.code == 0
    assert seen == []


def _schedule_env(tmp_path, monkeypatch, plist):
    from cpool import accounts, launchd

    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    if plist:
        (home / "Library" / "LaunchAgents" / f"{launchd.WARM}.plist").write_text("")
    monkeypatch.setattr(cli.Path, "home", lambda: home)
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[[account]]\nname = "main"\ndir = "default"\norder = 1\n')
    installed = []
    monkeypatch.setattr(launchd, "install", lambda cfg, binary: installed.append(binary))
    monkeypatch.setattr(launchd, "pmset_apply", lambda cfg: "sudo pmset repeat wakeorpoweron X")
    fake = lambda *a, **k: type("A", (), {"name": "x"})()  # noqa: E731
    monkeypatch.setattr(accounts, "add", fake)
    monkeypatch.setattr(accounts, "adopt", fake)
    monkeypatch.setattr(accounts, "remove", lambda name: None)
    return installed


@pytest.mark.parametrize("argv", [["add", "x"], ["adopt", "/d", "--name", "x"], ["remove", "x"]])
@pytest.mark.parametrize("plist", [True, False])
def test_account_changes_refresh_installed_schedule(tmp_path, monkeypatch, capsys, argv, plist):
    installed = _schedule_env(tmp_path, monkeypatch, plist)
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert len(installed) == int(plist)
    assert ("schedule updated" in out and "sudo pmset" in out) == plist


def _replan_env(tmp_path, monkeypatch, plist=True):
    from cpool import config, launchd, schedule, state

    _schedule_env(tmp_path, monkeypatch, plist)
    active = {"names": ["main"]}
    monkeypatch.setattr(
        schedule, "active", lambda cfg, st: [type("A", (), {"name": n})() for n in active["names"]]
    )
    calls = []
    monkeypatch.setattr(launchd, "install", lambda cfg, binary, only=(): calls.append(only))
    monkeypatch.setattr(cli, "_cpool_bin", lambda: "/x/bin/cpool")
    return config.load(), active, calls, state


def test_replan_first_call_stores_active_without_install(tmp_path, monkeypatch):
    cfg, _, calls, state = _replan_env(tmp_path, monkeypatch)
    cli._replan_if_active_changed(cfg)
    assert state.read()["plan_active"] == ["main"]
    assert calls == []


def test_replan_changed_active_reinstalls_warm_only(tmp_path, monkeypatch):
    from cpool import launchd

    cfg, active, calls, state = _replan_env(tmp_path, monkeypatch)
    cli._replan_if_active_changed(cfg)
    active["names"] = ["main", "claude2"]
    cli._replan_if_active_changed(cfg)
    assert calls == [(launchd.WARM,)]
    assert state.read()["plan_active"] == ["main", "claude2"]


def test_replan_retries_after_install_failure(tmp_path, monkeypatch):
    from cpool import launchd

    cfg, active, calls, state = _replan_env(tmp_path, monkeypatch)
    cli._replan_if_active_changed(cfg)
    active["names"] = ["claude2"]
    attempts = []

    def flaky(cfg, binary, only=()):
        attempts.append(only)
        if len(attempts) == 1:
            raise OSError("launchctl failed")

    monkeypatch.setattr(launchd, "install", flaky)
    with pytest.raises(OSError):
        cli._replan_if_active_changed(cfg)
    assert state.read()["plan_active"] == ["main"]
    cli._replan_if_active_changed(cfg)
    assert len(attempts) == 2
    assert state.read()["plan_active"] == ["claude2"]
