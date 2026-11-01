"""The schedule decides when paid accounts get spent; plan/due are pure logic with branches."""
from datetime import datetime
from pathlib import Path

from cpool import schedule
from cpool.config import Account, Config, Pool


def cfg(n: int) -> Config:
    accts = tuple(Account(f"a{i}", Path(f"/x/{i}"), i + 1) for i in range(n))
    return Config(Pool(start_hour=8), accts)


def times(c: Config, name: str) -> list[str]:
    return [f"{s.hour:02d}:{s.minute:02d}" for s in schedule.plan(c) if s.account == name]


def test_plan_one_account():
    assert times(cfg(1), "a0") == ["03:02", "08:02", "13:02", "18:02"]


def test_plan_two_accounts():
    assert times(cfg(2), "a1") == ["05:32", "10:32", "15:32", "20:32"]


def test_plan_three_accounts_step_90():
    c = cfg(3)
    # resets 8:00, 9:30, 11:00 -> warm 5h earlier + 2 min
    assert [times(c, f"a{i}")[0] for i in range(3)] == ["03:02", "04:32", "06:02"]


NOW = datetime(2026, 11, 2, 8, 30)  # 28 min after a0's 08:02 slot


def test_due_fires_for_recent_slot():
    assert schedule.due(cfg(1), {}, NOW) == ["a0"]


def test_due_skips_old_slot():
    assert schedule.due(cfg(1), {}, datetime(2026, 11, 2, 9, 5)) == []


def test_due_skips_open_window():
    st = {"accounts": {"a0": {"five_hour": {"resets_at": NOW.timestamp() + 600}}}}
    assert schedule.due(cfg(1), st, NOW) == []
    st["accounts"]["a0"]["five_hour"]["resets_at"] = NOW.timestamp() - 1
    assert schedule.due(cfg(1), st, NOW) == ["a0"]


def test_due_skips_live_session_and_just_warmed():
    live = {"sessions": {"s": {"account": "a0", "updated_at": NOW.timestamp() - 30}}}
    assert schedule.due(cfg(1), live, NOW) == []
    old = {"sessions": {"s": {"account": "a0", "updated_at": NOW.timestamp() - 600}}}
    assert schedule.due(cfg(1), old, NOW) == ["a0"]
    warmed = {"accounts": {"a0": {"warmed_at": NOW.timestamp() - 600}}}
    assert schedule.due(cfg(1), warmed, NOW) == []


def test_failed_warm_retried_after_15_min_not_before():
    t = NOW.timestamp()
    soon = {"accounts": {"a0": {"warm_failed_at": t - 14 * 60}}}
    assert schedule.due(cfg(1), soon, NOW) == []
    later = {"accounts": {"a0": {"warm_failed_at": t - 16 * 60}}}
    assert schedule.due(cfg(1), later, NOW) == ["a0"]


def test_due_aware_now_uses_local_slots():
    from datetime import timezone
    assert schedule.due(cfg(1), {}, NOW.astimezone(timezone.utc)) == ["a0"]
