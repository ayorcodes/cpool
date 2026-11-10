"""Shared display contract: both surfaces must agree on usage and reset semantics."""

import pytest

from cpool import render


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


def test_plain_account_rows_preserve_active_auth_and_expired_windows(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert render.color_enabled() is False
    acct = {"five_hour": {"used": 80, "resets_at": 99}, "auth": "expired"}
    active = render.account_line("a", acct, 100, active=True, name_width=1, color=False)
    inactive = render.account_line("a", {}, 100, active=False, name_width=1, color=False)
    assert active == "● a  5h ▱▱▱▱▱▱▱▱   0%         7d ▱▱▱▱▱▱▱▱   0%        ⚠ expired"
    assert inactive == "○ a  5h ▱▱▱▱▱▱▱▱    –         7d ▱▱▱▱▱▱▱▱    –"
    assert "\x1b" not in active + inactive
    assert "↻" not in active
