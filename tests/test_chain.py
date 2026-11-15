import pytest

from cpool import chain, config


def account(used=40, week=60, reset=4600, **extra):
    return {"updated_at": 1000, "credits_enabled": False,
            "five_hour": {"used": used, "resets_at": reset},
            "seven_day": {"used": week, "resets_at": 303400}, **extra}


def test_weighted_room_burn_reset_and_weekly():
    cfg = config.Config(config.Pool(), (config.Account("a", None, 0, capacity=2),
                                       config.Account("b", None, 1, capacity=1)))
    st = {"accounts": {"a": account(), "b": account(90)}, "sessions": {
        "live": {"account": "a", "updated_at": 1000, "turns": [
            {"ts": 100, "u5h": 10}, {"ts": 400, "u5h": 20},
            {"ts": 700, "u5h": 40}, {"ts": 1700, "u5h": 90}]},
        "stale": {"account": "a", "updated_at": 399, "turns": [
            {"ts": 100, "u5h": 1}, {"ts": 200, "u5h": 50}, {"ts": 300, "u5h": 90}]},
        "single": {"account": "b", "updated_at": 1000, "turns": [
            {"ts": 100, "u5h": 10}, {"ts": 400, "u5h": 20}]}}}
    result = chain.summary(cfg, st, 1000)
    assert result.room == pytest.approx(.65)
    assert result.total == 1.5
    assert result.room_ready == .6
    assert result.burn == pytest.approx(1.8)
    assert result.runway_s == pytest.approx(1300)
    assert (result.next_at, result.next_account, result.next_gain) == (4600, "a", .4)
    assert not result.seamless
    assert result.gap_at == pytest.approx(2300)
    assert result.gap_s == pytest.approx(2300)
    assert result.week_used == 60
    assert result.week_delta == 10
    st["accounts"]["a"]["five_hour"]["resets_at"] = 2000
    assert chain.summary(cfg, st, 1000).seamless
    st["accounts"]["a"]["seven_day"]["used"] = 0
    assert chain.summary(cfg, st, 1000).week_delta == -30


@pytest.mark.parametrize("extra", [{"credits_enabled": True}, {"auth": "expired"}, {"updated_at": None}])
def test_excluded_accounts_and_missing_samples(extra):
    cfg = config.Config(config.Pool(), (config.Account("a", None, 0), config.Account("b", None, 1)))
    result = chain.summary(cfg, {"accounts": {"a": account(**extra), "b": account(left_at=999)}}, 1000)
    assert (result.room, result.total, result.room_ready) == (.6, 1, 0)
    assert result.burn is None and result.runway_s is None
    assert chain.summary(cfg, {}, 1000) is None


def test_no_counted_accounts_or_future_resets():
    cfg = config.Config(config.Pool(), (config.Account("a", None, 0),))
    result = chain.summary(cfg, {"accounts": {"a": account(auth="expired")}}, 1000)
    assert result.total == 0 and result.week_used == 0
    assert result.next_at is None and result.week_delta is None
    result = chain.summary(cfg, {"accounts": {"a": {**account(reset=999), "seven_day": {}}}}, 1000)
    assert result.room == 1 and result.next_at is None and result.week_delta is None
