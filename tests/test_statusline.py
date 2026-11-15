"""Shard routing must not attribute proxy usage to the launch account."""
import io
import json
import os
import sys

import pytest

from cpool import config, render, shardacct, state, statusline, usage


def test_live_by_account_mixed_routes_dedupes_processes(tmp_path, monkeypatch):
    from collections import Counter
    monkeypatch.setattr(state, "pid_alive", lambda pid: pid in {1, 2, 3, 4, 5})
    monkeypatch.setattr(shardacct, "read_session_auth", lambda: {"shard": "auth1", "unknown": "abcdefghi"})
    monkeypatch.setattr(shardacct, "auth_emails", lambda now: {"auth1": "b@example.test"})
    monkeypatch.setattr(shardacct, "account_emails", lambda cfg: {"b@example.test": "b"})
    cfg = config.Config(config.Pool(), (config.Account("a", tmp_path / "a", 0),
                                       config.Account("b", tmp_path / "b", 1, enabled=False)))
    st = {"accounts": {"b": {"auth": "expired"}}, "sessions": {
        "normal": {"pid": 1, "account": "a"},
        "old": {"pid": 2, "account": "a", "updated_at": 1},
        "shard": {"pid": 2, "account": "a", "shard": True, "updated_at": 2},
        "unknown": {"pid": 3, "account": "a", "shard": True},
        "noauth": {"pid": 4, "account": "a", "shard": True},
        "disabled": {"pid": 5, "account": "b"},
        "dead": {"pid": 99, "account": "a"},
        "none": {"pid": None, "account": "a"}}}
    assert shardacct.live_by_account(cfg, st, 1000) == (Counter({"a": 1, "b": 2}),
                                                     Counter({"#abcdef": 1, "…": 1}))
    monkeypatch.delenv("CPOOL_SHARD", raising=False)
    lines = render.account_lines(cfg, st, "a", 1000, color=False)
    assert any("b  ●●         2" in row and "expired" in row for row in lines)


@pytest.mark.parametrize("shard", [False, True])
def test_statusline_route_preserves_account_usage(tmp_path, monkeypatch, capsys, shard):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("CPOOL_RUN", "run-1")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("CPOOL_STATUSLINE_NEXT", raising=False)
    if shard:
        monkeypatch.setenv("CPOOL_SHARD", "1")
    else:
        monkeypatch.delenv("CPOOL_SHARD", raising=False)
    monkeypatch.setattr(statusline.time, "time", lambda: 1000)
    monkeypatch.setattr(usage, "kick", lambda cfg, now: False)
    monkeypatch.setattr(statusline, "_ancestor_claude_pid", os.getpid)
    config.save_account(config.Account("a", tmp_path / "a", 1))
    stored = {"five_hour": {"used": 25, "resets_at": 4600},
              "seven_day": {"used": 50, "resets_at": 87400},
              "source": "poll", "auth": "expired", "updated_at": 900}
    with state.locked() as st:
        st["accounts"]["a"] = stored
        st["sessions"]["s1"] = {"shard": True, "pid": os.getpid()}
    payload = {"session_id": "s1", "model": {"id": "claude-opus-5-5"},
               "workspace": {"current_dir": "/w"}, "effort": {"level": "high"},
               "context_window": {"used_percentage": 18}, "prompt_cache": {"read": 3},
               "rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": 4600},
                               "seven_day": {"used_percentage": 60, "resets_at": 87400}}}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert statusline.main() == 0
    output = capsys.readouterr().out.splitlines()
    assert output[-1].startswith("pool ")
    account_output = "\n".join(output[:-1]) + "\n"
    st = state.read()
    if shard:
        assert st["accounts"]["a"] == stored
        assert st["sessions"]["s1"]["shard"] is True
        assert st["sessions"]["s1"]["turns"] == []
        assert account_output == (
            "⇄ shard 1 · …×1\n"
            "  a  ·          0  5h ▰▰▱▱▱▱▱▱  25% ↻1h00m  7d ▰▰▰▰▱▱▱▱  50% ↻1d0h  ⚠ expired\n")
    else:
        assert st["accounts"]["a"] == {
            "five_hour": {"used": 30, "resets_at": 4600},
            "seven_day": {"used": 60, "resets_at": 87400},
            "source": "statusline", "auth": "ok", "updated_at": 1000}
        assert not st["sessions"]["s1"].get("shard")
        assert st["sessions"]["s1"]["turns"] == [{"ts": 1000, "u5h": 30}]
        assert account_output == (
            "▸ a  ●          1  5h ▰▰▱▱▱▱▱▱  30% ↻1h00m  7d ▰▰▰▰▰▱▱▱  60% ↻1d0h\n")
    session = st["sessions"]["s1"]
    assert (session["account"], session["run"], session["pid"], session["cwd"], session["model"],
            session["effort"], session["updated_at"], session["started_at"], session["ctx_used"], session["cache"]) == (
                "a", "run-1", os.getpid(), "/w", "claude-opus-5-5", "high", 1000, 1000, 18, {"read": 3})


@pytest.mark.parametrize("stale_leave", [False, True])
def test_shard_statusline_never_leaves_busy_launch_account(tmp_path, monkeypatch, stale_leave):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("CPOOL_RUN", "run-1")
    monkeypatch.setenv("CPOOL_SHARD", "1")
    monkeypatch.delenv("CPOOL_STATUSLINE_NEXT", raising=False)
    monkeypatch.setattr(statusline.time, "time", lambda: 1000)
    monkeypatch.setattr(usage, "kick", lambda cfg, now: False)
    monkeypatch.setattr(statusline, "_ancestor_claude_pid", os.getpid)
    for priority, name in enumerate(("a", "b"), 1):
        config.save_account(config.Account(name, tmp_path / name, priority))
    with state.locked() as st:
        for name, used in (("a", 99), ("b", 10)):
            st["accounts"][name] = {
                "five_hour": {"used": used, "resets_at": 4600},
                "auth": "ok", "updated_at": 1000, "credits_enabled": False}
        if stale_leave:
            st["sessions"]["s1"] = {
                "pid": os.getpid(), "leave": "soft", "leave_blocked": "none-eligible: b"}
    payload = {"session_id": "s1", "model": {"id": "claude-opus-5-5"}}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert statusline.main() == 0
    session = state.read()["sessions"]["s1"]
    assert session["shard"] is True
    assert "leave" not in session
    assert "leave_blocked" not in session


@pytest.mark.parametrize("leave,leave_manual,deferred,expected", [("soft", False, 1, "leave deferred: 1 agent"),
    ("soft", False, 2, "leave deferred: 2 agents"), (None, True, 2, "leave deferred: 2 agents"),
    (None, False, 2, None), ("soft", False, 0, None)])
def test_deferred_leave_status(tmp_path, monkeypatch, capsys, leave, leave_manual, deferred, expected):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CPOOL_RUN", "r")
    monkeypatch.delenv("CPOOL_STATUSLINE_NEXT", raising=False)
    monkeypatch.setattr(statusline, "_record", lambda payload: None)
    monkeypatch.setattr(usage, "kick", lambda *a: None)
    config.save_account(config.Account("a", tmp_path / "a", 1))
    with state.locked() as st:
        st["sessions"]["s"] = {"pid": os.getpid(), "updated_at": 1, "leave": leave,
                                "leave_manual": leave_manual}
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "r.json").write_text(json.dumps({"version": 1, "agents": {}, "deferred": deferred}))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id":"s"}')))
    assert statusline.main() == 0
    out = capsys.readouterr().out
    if expected:
        assert expected in out
    else:
        assert "leave deferred" not in out


@pytest.mark.parametrize("key", ["five_hour", "seven_day"])
def test_statusline_preserves_reset_rollover(tmp_path, monkeypatch, key):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "a"))
    monkeypatch.delenv("CPOOL_SHARD", raising=False)
    monkeypatch.setattr(statusline.time, "time", lambda: 1000)
    config.save_account(config.Account("a", tmp_path / "a", 1))
    with state.locked() as st:
        st["accounts"]["a"] = {key: {"used": 90, "resets_at": 900}}
    statusline._record({"rate_limits": {key: {"used_percentage": 0, "resets_at": 2000}}})
    assert state.read()["accounts"]["a"][key] == {
        "used": 0, "resets_at": 2000, "prev_resets_at": 900}
