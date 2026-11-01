"""status reads the persisted state contract that other processes write."""

import json
import os
import time

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
