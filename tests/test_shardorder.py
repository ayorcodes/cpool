"""Priority ordering crosses the management API; existing attribution tests never PATCH."""
import json
from dataclasses import replace
from urllib.error import HTTPError

import pytest

from cpool import config, shardacct, shardorder, state


def account(used=20, reset=None, **extra):
    return {"credits_enabled": False, "updated_at": 1000,
            "five_hour": {"used": used, "resets_at": reset}, **extra}


def test_order_reset_score_eligibility_and_cooldown():
    cfg = config.Config(config.Pool(), tuple(config.Account(n, None, i) for i, n in enumerate(
        ["limited", "missing", "later", "soon", "high", "low", "off"])))
    cfg = replace(cfg, accounts=cfg.accounts[:-1] + (replace(cfg.accounts[-1], enabled=False),))
    st = {"accounts": {"limited": account(90, 4000), "later": account(5, 3000),
                       "soon": account(60, 2000, left_at=999),
                       "high": account(5, seven_day={"used": 5, "resets_at": 9000}),
                       "low": account(60, seven_day={"used": 70, "resets_at": 9000})}}
    assert shardorder.order(st, cfg, 1000) == ["soon", "later", "high", "low", "limited", "missing"]
    st["accounts"]["high"]["five_hour"]["resets_at"] = 3000
    assert shardorder.order(st, cfg, 1000)[:3] == ["soon", "high", "later"]


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setattr(shardacct, "get_key", lambda: "key")
    monkeypatch.setattr(shardacct, "account_emails", lambda cfg: {"a@example.com": "a", "b@example.com": "b", "c@example.com": "c"})
    st = {"accounts": {"a": account(10, 2000), "b": account(20, 3000), "c": account(30, 4000)}}
    monkeypatch.setattr(state, "read", lambda: st)
    cfg = config.Config(config.Pool(), (config.Account("a", None, 0), config.Account("b", None, 1), config.Account("c", None, 2)),
                        shard=config.Shard(True))
    calls, logs = [], []
    monkeypatch.setattr(shardorder.journal, "log", lambda kind, **kw: logs.append((kind, kw)))
    def mgmt(path, key, **kw):
        calls.append((path, kw))
        if not kw:
            return {"files": [{"name": "a.json", "type": "claude", "email": "A@example.com"},
                              {"name": "b.json", "type": "claude", "email": "b@example.com"},
                              {"name": "c.json", "type": "claude", "email": "c@example.com"},
                              {"name": "other", "type": "codex", "email": "a@example.com"},
                              {"name": "unknown", "type": "claude", "email": "x@example.com"}]}
    monkeypatch.setattr(shardacct, "mgmt", mgmt)
    return cfg, st, calls, logs, tmp_path / "shard-order.json", mgmt


def test_sync_priorities_cache_and_change(sync_env):
    cfg, st, calls, logs, path, _ = sync_env
    assert shardorder.sync(cfg, 1000) is True
    assert [kw["body"] for _, kw in calls if kw] == [
        {"name": "a.json", "priority": 100}, {"name": "b.json", "priority": 99}, {"name": "c.json", "priority": 98}]
    assert json.loads(path.read_text())["priorities"] == {"a.json": 100, "b.json": 99, "c.json": 98}
    assert logs[-1][0] == "shard-order"
    calls.clear()
    assert shardorder.sync(cfg, 1000) is False
    assert len(calls) == 1
    st["accounts"]["b"]["five_hour"]["resets_at"] = 1500
    calls.clear()
    assert shardorder.sync(cfg, 1000) is True
    assert [kw["body"] for _, kw in calls if kw] == [
        {"name": "a.json", "priority": 99}, {"name": "b.json", "priority": 100}]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_sync_failure_latch_and_new_key(sync_env, monkeypatch, status):
    cfg, _, calls, logs, path, mgmt = sync_env
    def failing(path, key, **kw):
        if kw and kw["body"]["name"] == "b.json":
            raise HTTPError("http://local", status, "failure", {}, None)
        return mgmt(path, key, **kw)
    monkeypatch.setattr(shardacct, "mgmt", failing)
    assert shardorder.sync(cfg, 1000) is False
    assert logs[-1][0] == "shard-order-error"
    assert logs[-1][1]["status"] == status
    if status == 500:
        assert not path.exists()
        return
    assert json.loads(path.read_text())["rejected"] is True
    calls.clear()
    assert shardorder.sync(cfg, 1000) is False
    assert calls == []
    monkeypatch.setattr(shardacct, "get_key", lambda: "new-key")
    monkeypatch.setattr(shardacct, "mgmt", mgmt)
    assert shardorder.sync(cfg, 1000) is True


def test_sync_disabled_or_missing_key(sync_env, monkeypatch):
    cfg, _, calls, _, _, _ = sync_env
    assert shardorder.sync(replace(cfg, shard=config.Shard(False)), 1000) is False
    monkeypatch.setattr(shardacct, "get_key", lambda: None)
    assert shardorder.sync(cfg, 1000) is False
    assert calls == []


@pytest.mark.parametrize("raw", [None, "invalid", 42, {}])
def test_sync_invalid_response_saves_nothing(sync_env, monkeypatch, raw):
    cfg, _, _, logs, path, _ = sync_env
    monkeypatch.setattr(shardacct, "mgmt", lambda *args, **kwargs: raw)
    assert shardorder.sync(cfg, 1000) is False
    assert not path.exists()
    assert logs == [("shard-order-error", {"stage": "fetch", "error": "ValueError", "status": None})]


@pytest.mark.parametrize("text", ["garbage", "null", "[]", '{}',
                                  '{"key_fp":"key","priorities":[],"rejected":false}'])
def test_sync_invalid_cache_is_replaced(sync_env, text):
    cfg, _, calls, logs, path, _ = sync_env
    path.write_text(text)
    assert shardorder.sync(cfg, 1000) is True
    assert [kw["body"] for _, kw in calls if kw] == [
        {"name": "a.json", "priority": 100}, {"name": "b.json", "priority": 99},
        {"name": "c.json", "priority": 98}]
    assert json.loads(path.read_text())["priorities"] == {"a.json": 100, "b.json": 99, "c.json": 98}
    assert logs[-1][0] == "shard-order"
