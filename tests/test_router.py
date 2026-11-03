"""Routing decides which paid account a session spends; the branches are the contract."""
from pathlib import Path

import pytest

from cpool.config import Account, Config, Pool
from cpool.router import choose, score, should_leave

NOW = 1_000_000
FAR = NOW + 4 * 3600


def acct(u5=0.0, u7=0.0, tier="default_claude_max_5x", **kw):
    a = {
        "five_hour": {"used": u5, "resets_at": FAR},
        "seven_day": {"used": u7, "resets_at": NOW + 86400 * 3},
        "credits_enabled": False, "tier": tier, "auth": "ok", "updated_at": NOW - 60,
    }
    a.update(kw)
    return a


def cfg():
    return Config(Pool(), (Account("main", Path("/m"), 1), Account("pro", Path("/p"), 2)))


def run(accounts, **kw):
    return choose({"accounts": accounts, "sessions": kw.pop("sessions", {})}, cfg(),
                  model=kw.pop("model", "claude-opus-5-5"), cwd="/w", now=NOW, **kw)


CASES = {
    "max5x_70_beats_pro_20": (
        dict(main=acct(70), pro=acct(20, tier="default_claude_ai")), {}, "main", "best"),
    "credits_unknown_excluded": (
        dict(main=acct(10, credits_enabled=None), pro=acct(80, tier="default_claude_ai")), {}, "pro", "best"),
    "model_week_excludes_only_family": (
        dict(main=acct(10, model_week={"opus": {"used": 95, "resets_at": FAR}}), pro=acct(50)),
        {}, "pro", "best"),
    "model_week_other_family_ok": (
        dict(main=acct(10, model_week={"opus": {"used": 95, "resets_at": FAR}}), pro=acct(50)),
        dict(model="claude-sonnet-5"), "main", "best"),
    "sticky_within_margin": (
        dict(main=acct(30), pro=acct(25)), dict(current="main"), "main", "sticky"),
    "margin_beaten": (
        dict(main=acct(80), pro=acct(0)), dict(current="main"), "pro", "margin"),
    "forced_excludes_current": (
        dict(main=acct(0), pro=acct(50)), dict(current="main", forced=True), "pro", "best"),
    "cooldown": (
        dict(main=acct(0, left_at=NOW - 60), pro=acct(50)), {}, "pro", "best"),
    "cooldown_ignored_when_manual": (
        dict(main=acct(0, left_at=NOW - 60), pro=acct(50)), dict(ignore_cooldown=True), "main", "best"),
    "ignore_cooldown_keeps_other_rules": (
        dict(main=acct(95, left_at=NOW - 60), pro=acct(50)), dict(ignore_cooldown=True), "pro", "best"),
}


@pytest.mark.parametrize("accounts,kw,want,reason", CASES.values(), ids=CASES.keys())
def test_choose(accounts, kw, want, reason):
    d = run(accounts, **kw)
    assert (d.account, d.reason) == (want, reason)


def test_none_eligible_reports_earliest_reset():
    soon = NOW + 1800
    d = run({"main": acct(90, five_hour={"used": 90, "resets_at": FAR}),
             "pro": acct(95, five_hour={"used": 95, "resets_at": soon})})
    assert d.account is None and d.reason.startswith("none-eligible") and d.earliest_reset == soon


def test_should_leave_projected_uses_median_of_positive_deltas():
    sess = {"model": "claude-opus-5-5", "turns": [{"u5h": v} for v in (80, 84, 84, 90, 93)]}
    # positive deltas 4, 6, 3 -> median 4
    p = Pool(soft=95, projected=97)
    assert should_leave(sess, acct(92), p, NOW) is None
    assert should_leave(sess, acct(93), p, NOW) == "projected"
    assert should_leave(sess, acct(95), p, NOW) == "soft"
    assert should_leave({"model": "claude-opus-5-5"}, acct(10, limited={"model": "opus", "until": NOW + 5}),
                        Pool(), NOW) == "model"


def test_none_eligible_names_why_each_account_was_skipped():
    d = run(dict(main=acct(0, left_at=NOW - 60), pro=acct(0)), current="pro", forced=True)
    assert d.account is None
    assert d.skipped == {"main": f"cooldown:{NOW - 60 + 300}", "pro": "current"}


def test_weekly_pacing_prefers_account_behind_its_pace():
    # equal 5h headroom and weekly use; main's week ends in 1 day (behind pace), pro's in 6 days (ahead)
    d = run(dict(main=acct(30, 40, seven_day={"used": 40, "resets_at": NOW + 86400}),
                 pro=acct(30, 40, seven_day={"used": 40, "resets_at": NOW + 86400 * 6})))
    assert d.account == "main"
    # main's reset is also inside the expiry window: +100 * 60% unused
    assert d.scores["main"] - d.scores["pro"] == pytest.approx(
        0.3 * (100 * (6 / 7) - 40) - 0.3 * (100 * (1 / 7) - 40) + 60)


@pytest.mark.parametrize("mins,bonus", [(0, 25), (60, 12.5), (120, 0), (240, 0)])
def test_expiring_window_bonus_fades_to_zero_at_two_hours(mins, bonus):
    a = acct(0, 0, five_hour={"used": 0, "resets_at": NOW + max(mins * 60, 1)}, seven_day=None)
    s = score("main", a, {}, cwd="/w", now=NOW, pool=Pool(), model="claude-opus-5-5")
    assert s == pytest.approx(95 + bonus, abs=0.1)


@pytest.mark.parametrize("pro_reset_h,want", [(25, "claude2"), (60, "main")])
def test_weekly_expiry_bonus_spends_allowance_about_to_expire(pro_reset_h, want):
    # a Pro week about to reset outweighs Max5x capacity scaling, but only inside the expiry window
    c = Config(Pool(), (Account("main", Path("/m"), 1), Account("claude2", Path("/p"), 2)))
    accounts = {
        "main": acct(5, 37, five_hour={"used": 5, "resets_at": NOW + 16920},
                     seven_day={"used": 37, "resets_at": NOW + 64 * 3600}),
        "claude2": acct(31, 33, tier="default_claude_ai", five_hour={"used": 31, "resets_at": NOW + 9720},
                        seven_day={"used": 33, "resets_at": NOW + pro_reset_h * 3600}),
    }
    d = choose({"accounts": accounts, "sessions": {}}, c, model="claude-opus-5-5", cwd="/w", now=NOW)
    assert d.account == want
