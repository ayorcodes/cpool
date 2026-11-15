"""Shared display contract: both surfaces must agree on usage and reset semantics."""

import pytest

from collections import Counter

from cpool import config, render, shardacct, state


def test_pool_lines_outlook_and_omissions(monkeypatch):
    import time
    monkeypatch.setattr(render.time, "localtime", lambda ts: time.gmtime(ts))
    cfg = config.Config(config.Pool(), (config.Account("a", None, 0),))
    st = {"accounts": {"a": {"updated_at": 1000, "credits_enabled": False,
          "five_hour": {"used": 40, "resets_at": 4600},
          "seven_day": {"used": 60, "resets_at": 303400}}},
          "sessions": {"s": {"account": "a", "updated_at": 1000, "turns": [
              {"ts": 100, "u5h": 10}, {"ts": 400, "u5h": 20}, {"ts": 700, "u5h": 30}]}}}
    rows = render.pool_lines(cfg, st, 1000, name_width=4, color=False)
    assert rows == [
        "  pool                5h ▰▰▰▱▱▱▱▱  40%         7d ▰▰▰▰▰▱▱▱  60%",
        "                      lasts ~30m · next reset a 01:16 · gap ~30m at 00:46",
    ]
    account = render.account_line("a", st["accounts"]["a"], 1000, active=True, name_width=4, color=False)
    assert rows[0].index("5h") == account.index("5h")
    st["accounts"]["a"]["five_hour"]["resets_at"] = 2000
    assert render.pool_lines(cfg, st, 1000, name_width=4, color=False)[1].endswith("no gap")
    st["sessions"] = {}
    st["accounts"]["a"]["five_hour"] = {}
    assert render.pool_lines(cfg, st, 1000, name_width=4, color=False) == [
        "  pool                5h ▱▱▱▱▱▱▱▱   0%         7d ▰▰▰▰▰▱▱▱  60%",
    ]
    st["accounts"]["a"]["left_at"] = 999
    assert render.pool_lines(cfg, st, 1000, name_width=4, color=False) == [
        "  pool                5h ▱▱▱▱▱▱▱▱   0%         7d ▰▰▰▰▰▱▱▱  60%",
    ]
    assert render.pool_lines(cfg, {}, 1000, name_width=4, color=False) == []
    st["accounts"]["a"]["auth"] = "expired"
    assert render.pool_lines(cfg, st, 1000, name_width=4, color=False) == []



@pytest.fixture(autouse=True)
def isolated_shard_environment(monkeypatch):
    monkeypatch.delenv("CPOOL_SHARD", raising=False)


@pytest.mark.parametrize("used, expected", [(0, "▱▱▱▱▱▱▱▱"), (49, "▰▰▰▰▱▱▱▱"),
                                            (50, "▰▰▰▰▱▱▱▱"), (100, "▰▰▰▰▰▰▰▰"),
                                            (130, "▰▰▰▰▰▰▰▰")])
def test_bar_clamps_and_rounds_usage(used, expected):
    assert render.bar(used) == expected


@pytest.mark.parametrize("reset, expected", [(3540, "↻59m"), (3600, "↻1h00m"),
                                             (86400, "↻1d0h"), (-1, ""), (None, "")])
def test_reset_countdown_boundaries(reset, expected):
    assert render.until(reset, 0) == expected


@pytest.mark.parametrize("used, expected", [(0, "ok"), (49, "ok"), (50, "warn"),
                                            (79, "warn"), (80, "hot"), (100, "hot")])
def test_usage_severity_thresholds(used, expected):
    assert render.level(used) == expected


@pytest.mark.parametrize("used, expected", [(130, "100%"), (-5, "0%")])
def test_window_cell_clamps_usage_without_changing_width(used, expected):
    cell = render.window_cell({"five_hour": {"used": used, "resets_at": 3600}}, "five_hour", 0, color=False)
    normal = render.window_cell({"five_hour": {"used": 50, "resets_at": 3600}}, "five_hour", 0, color=False)
    assert len(cell) == len(normal)
    assert cell.split()[2] == expected


def test_sessions_cell_width_and_stale_age():
    rows = [render.account_line("main", {"updated_at": 1}, 240, active=True,
                                name_width=4, color=False, live=n) for n in (0, 1, 4, 9)]
    assert len({row.index("5h") for row in rows}) == 1
    for row, glyphs, count in zip(rows, ("·", "●", "●●●●", "●●●●+5"), (0, 1, 4, 9)):
        cell = row.split("5h")[0]
        assert glyphs in cell and str(count) in cell
        assert row.endswith("· 3m ago")
    fresh = render.account_line("main", {"updated_at": 200}, 240, active=True,
                                name_width=4, color=False)
    assert "ago" not in fresh


def test_plain_account_rows_preserve_active_auth_and_expired_windows(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert render.color_enabled() is False
    acct = {"five_hour": {"used": 80, "resets_at": 99}, "auth": "expired"}
    active = render.account_line("a", acct, 100, active=True, name_width=1, color=False)
    inactive = render.account_line("a", {}, 100, active=False, name_width=1, color=False)
    assert active == "▸ a  ·          0  5h ▱▱▱▱▱▱▱▱   0%         7d ▱▱▱▱▱▱▱▱   0%        ⚠ expired"
    assert inactive == "  a  ·          0  5h ▱▱▱▱▱▱▱▱    –         7d ▱▱▱▱▱▱▱▱    –"
    assert "\x1b" not in active + inactive
    assert "↻" not in active


@pytest.mark.parametrize("shard_env, sessions, header, marker", [
    (None, {}, [], "▸"),
    ("0", {"dead": {"shard": True, "pid": 0}}, [], "▸"),
    ("1", {}, ["⇄ shard 0"], " "),
    ("1", {"live": {"shard": True, "pid": 42},
           "dead": {"shard": True, "pid": 0},
           "normal": {"pid": 42}}, ["⇄ shard 1 · …×1"], " "),
    ("1", {"live": {"shard": True, "pid": 42},
           "other": {"shard": True, "pid": 42},
           "normal": {"shard": False, "pid": 42}}, ["⇄ shard 1 · …×1"], " "),
    (None, {"live": {"shard": True, "pid": 42},
            "other": {"shard": True, "pid": 42},
            "normal": {"shard": False, "pid": 42}}, ["⇄ shard 1 · …×1"], "▸"),
    ("0", {"live": {"shard": True, "pid": 42}}, ["⇄ shard 1 · …×1"], "▸"),
])
def test_account_lines_route_snapshot(tmp_path, monkeypatch, shard_env, sessions, header, marker):
    if shard_env is None:
        monkeypatch.delenv("CPOOL_SHARD", raising=False)
    else:
        monkeypatch.setenv("CPOOL_SHARD", shard_env)
    monkeypatch.setattr(state, "pid_alive", lambda pid: pid == 42)
    monkeypatch.setattr(shardacct, "read_session_auth", lambda: {})
    cfg = config.Config(config.Pool(), (config.Account("a", tmp_path / "a", 1),
                                       config.Account("off", tmp_path / "off", 2, enabled=False)))
    st = {"accounts": {"a": {"five_hour": {"used": 25, "resets_at": 3600},
                                "seven_day": {"used": 50, "resets_at": 86400}}},
          "sessions": sessions}
    assert render.account_lines(cfg, st, "a", 0, color=False) == header + [
        marker + " a  ·          0  5h ▰▰▱▱▱▱▱▱  25% ↻1h00m  7d ▰▰▰▰▱▱▱▱  50% ↻1d0h"]
    assert render.account_line("a", st["accounts"]["a"], 0, active=True, name_width=1, color=False,
                               serving=set() if header else None) == (
        marker + " a  ·          0  5h ▰▰▱▱▱▱▱▱  25% ↻1h00m  7d ▰▰▰▰▱▱▱▱  50% ↻1d0h")


@pytest.mark.parametrize("shard_env, markers", [("1", (" ", "▸")), ("0", ("▸", " ")),
                                                (None, ("▸", " "))])
def test_serving_header_sort_and_rows(tmp_path, monkeypatch, shard_env, markers):
    """Only shard terminals mark serving accounts instead of their own account."""
    if shard_env is None:
        monkeypatch.delenv("CPOOL_SHARD", raising=False)
    else:
        monkeypatch.setenv("CPOOL_SHARD", shard_env)
    monkeypatch.setattr(state, "pid_alive", lambda pid: True)
    monkeypatch.setattr(shardacct, "serving", lambda *args: (Counter({"z": 1, "b": 1}), {"b"}))
    cfg = config.Config(config.Pool(), (config.Account("a", tmp_path / "a", 0),
                                       config.Account("b", tmp_path / "b", 1)))
    lines = render.account_lines(cfg, {"sessions": {"s": {"shard": True, "pid": 1}}}, "a", 0, color=False)
    assert lines[0] == "⇄ shard 1 · z×1"
    assert lines[1].startswith(markers[0] + " a ")
    assert lines[2].startswith(markers[1] + " b ")


@pytest.mark.parametrize("minutes, expected", [(300, "5h"), (10080, "7d"), (43200, "30d"),
                                               (90, "90m"), (45, "45m")])
def test_codex_window_labels(minutes, expected):
    assert render.window_label(minutes) == expected


@pytest.mark.parametrize("snap, now, expected", [
    ({"plan": "plus", "windows": [(300, 25, 3600), (10080, 42, 86400)], "ts": 0}, 0,
     "  codex                5h ▰▰▱▱▱▱▱▱  25% ↻1h00m  7d ▰▰▰▱▱▱▱▱  42% ↻1d0h  plus"),
    ({"plan": "free", "windows": [(43200, 10, 3600)], "ts": 0}, 0,
     "  codex                30d ▰▱▱▱▱▱▱▱  10% ↻1h00m free"),
    ({"plan": "plus", "windows": [(300, 80, 99)], "ts": 0}, 7200,
     "  codex                5h ▱▱▱▱▱▱▱▱   0%        plus · 2h ago"),
    ({"plan": "plus", "windows": [], "ts": None}, 0,
     "  codex                5h ▱▱▱▱▱▱▱▱    –         7d ▱▱▱▱▱▱▱▱    –        plus"),
])
def test_plain_codex_rows(snap, now, expected):
    assert render.codex_line(snap, now, name_width=5, color=False) == expected
