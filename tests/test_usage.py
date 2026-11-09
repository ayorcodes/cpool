"""Contract with the external usage endpoint's shape, and the statusline -> state write."""
import http.client
import io
import json
import os
import sys
from pathlib import Path

from cpool import accounts, config, router, state, statusline, usage

FIX = Path(__file__).parent / "fixtures"


def test_normalize_real_response():
    body = json.loads((FIX / "usage_claude2.json").read_text())
    out = usage.normalize(body, 1000, "default_claude_ai")
    assert out["five_hour"] == {"used": 0.0, "resets_at": 1793543399}
    assert out["seven_day"]["used"] == 29.0
    assert out["model_week"] == {}
    assert out["credits_enabled"] is False
    assert [x["kind"] for x in out["limits"]] == ["session", "weekly_all"]
    assert (out["tier"], out["source"], out["auth"], out["updated_at"]) == ("default_claude_ai", "poll", "ok", 1000)

    body["seven_day_opus"] = {"utilization": 30, "resets_at": "2026-11-06T01:00:00+00:00"}
    body["spend"]["enabled"] = True
    out = usage.normalize(body, 1000)
    assert out["model_week"]["opus"]["used"] == 30.0 and out["credits_enabled"] is True
    assert usage.normalize({}, 1000)["credits_enabled"] is None


def test_poll_429_backs_off_and_keeps_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    acct = config.Account("a", tmp_path / "a", 1)
    cfg = config.Config(config.Pool(), (acct,))
    with state.locked() as st:
        st["accounts"]["a"] = {"five_hour": {"used": 42.0, "resets_at": 5}, "left_at": 7}

    def boom(_):
        raise usage.UsageError(429)

    monkeypatch.setattr(usage, "fetch", boom)
    monkeypatch.setattr(usage, "_oauth", lambda a: {"accessToken": "tok1"})
    usage.poll(cfg, 10_000)
    a = state.read()["accounts"]["a"]
    assert a["five_hour"]["used"] == 42.0 and a["left_at"] == 7
    assert a["poll_backoff_until"] == 10_000 + 600
    usage.poll(cfg, 10_000 + 601)
    assert state.read()["accounts"]["a"]["poll_backoff_s"] == 1200


def _setup_statusline(tmp_path, monkeypatch, now=1_000_000):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "acct"))
    monkeypatch.setenv("CPOOL_RUN", "run-1")
    monkeypatch.delenv("CPOOL_STATUSLINE_NEXT", raising=False)
    acct = config.Account("a", tmp_path / "acct", 1)
    config.save_account(acct)
    monkeypatch.setattr(accounts, "resolve_account", lambda cfg, d: cfg.accounts[0])
    monkeypatch.setattr(router, "should_leave", lambda s, a, pool, now: None)
    monkeypatch.setattr(statusline.time, "time", lambda: now)


def _render(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert statusline.main() == 0


def test_statusline_stale_window_keeps_stored(tmp_path, monkeypatch):
    now = 1_000_000
    _setup_statusline(tmp_path, monkeypatch, now)
    with state.locked() as st:
        st["accounts"]["a"] = {"five_hour": {"used": 60.0, "resets_at": now + 9000},
                               "seven_day": {"used": 10.0, "resets_at": now + 90000},
                               "updated_at": 5, "source": "poll"}
    # expired five_hour (idle re-render) and an older seven_day window
    _render(monkeypatch, {"session_id": "s1", "rate_limits": {
        "five_hour": {"used_percentage": 95, "resets_at": now - 60},
        "seven_day": {"used_percentage": 2, "resets_at": now + 100}}})
    a = state.read()["accounts"]["a"]
    assert a["five_hour"] == {"used": 60.0, "resets_at": now + 9000}
    assert a["seven_day"] == {"used": 10.0, "resets_at": now + 90000}
    assert (a["updated_at"], a["source"]) == (5, "poll")
    assert state.read()["sessions"]["s1"]["updated_at"] == now

    # same resets_at is fresher within the window and is taken
    _render(monkeypatch, {"session_id": "s1", "rate_limits": {
        "five_hour": {"used_percentage": 70, "resets_at": now + 9000}}})
    a = state.read()["accounts"]["a"]
    assert a["five_hour"]["used"] == 70.0 and (a["updated_at"], a["source"]) == (now, "statusline")


def test_statusline_windowless_resets_keeps_live_stored_and_leave_manual(tmp_path, monkeypatch):
    now = 1_000_000
    _setup_statusline(tmp_path, monkeypatch, now)
    with state.locked() as st:
        st["accounts"]["a"] = {"five_hour": {"used": 60.0, "resets_at": now + 9000}, "updated_at": 5}
        st["sessions"]["s1"] = {"account": "a", "leave_manual": True, "pid": os.getpid()}
    _render(monkeypatch, {"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 1}}})
    st = state.read()
    assert st["accounts"]["a"]["five_hour"] == {"used": 60.0, "resets_at": now + 9000}
    assert st["sessions"]["s1"]["leave_manual"] is True  # same account: preserved
    with state.locked() as st:
        st["sessions"]["s1"].update(account="b", pid=os.getpid())
    _render(monkeypatch, {"session_id": "s1"})
    assert state.read()["sessions"]["s1"]["leave_manual"] is False  # account changed: cleared


def test_statusline_without_rate_limits_keeps_account_updated_at(tmp_path, monkeypatch):
    _setup_statusline(tmp_path, monkeypatch)
    with state.locked() as st:
        st["accounts"]["a"] = {"five_hour": {"used": 60.0, "resets_at": 1_009_000}, "updated_at": 5}
    _render(monkeypatch, {"session_id": "s1"})
    assert state.read()["accounts"]["a"]["updated_at"] == 5


def test_fetch_http_exception_is_network_error(monkeypatch):
    acct = config.Account("a", Path("/x"), 1)
    monkeypatch.setattr(usage, "_oauth", lambda a: {"accessToken": "t"})

    def boom(*a, **k):
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(usage.urllib.request, "urlopen", boom)
    try:
        usage.fetch(acct)
    except usage.UsageError as e:
        assert (e.status, str(e)) == (None, "network error")
    else:
        raise AssertionError("expected UsageError")


def test_poll_missing_credentials_marks_auth_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    cfg = config.Config(config.Pool(), (config.Account("a", tmp_path / "a", 1),))
    monkeypatch.setattr(usage, "_oauth", lambda a: None)
    usage.poll(cfg, 10_000)
    assert state.read()["accounts"]["a"]["auth"] == "missing"


def test_statusline_writes_state(tmp_path, monkeypatch, capsys):
    _setup_statusline(tmp_path, monkeypatch, 1_000_000)
    payload = {"session_id": "s1", "model": {"id": "m"}, "effort": {"level": "high"}, "workspace": {"current_dir": "/w"},
               "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": 1793543399},
                               "seven_day": {"used_percentage": 11, "resets_at": 1793900000}},
               "context_window": {"used_percentage": 18}, "prompt_cache": {"read": 3, "created": 1}}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert statusline.main() == 0
    st = state.read()
    assert st["accounts"]["a"]["five_hour"] == {"used": 40.0, "resets_at": 1793543399}
    assert st["accounts"]["a"]["source"] == "statusline"
    s = st["sessions"]["s1"]
    assert (s["account"], s["run"], s["cwd"], s["model"], s["ctx_used"]) == ("a", "run-1", "/w", "m", 18.0)
    assert s["effort"] == "high"
    assert s["cache"] == {"read": 3, "created": 1} and s["turns"][0]["u5h"] == 40.0
    assert capsys.readouterr().out.strip() == "cpool:a 5h 40% 7d 11%"


def _leave_setup(tmp_path, monkeypatch, other):
    now = 1_000_000
    _setup_statusline(tmp_path, monkeypatch, now)
    monkeypatch.setattr(router, "should_leave", lambda s, a, pool, n: "soft")
    config.save_account(config.Account("b", tmp_path / "b", 2))
    ok = {"five_hour": {"used": other, "resets_at": now + 9000}, "credits_enabled": False,
          "auth": "ok", "updated_at": now - 60}
    with state.locked() as st:
        st["accounts"]["b"] = ok
    _render(monkeypatch, {"session_id": "s1", "model": {"id": "claude-opus-5-5"}, "workspace": {"current_dir": "/w"},
                          "rate_limits": {"five_hour": {"used_percentage": 90, "resets_at": now + 9000}}})
    return state.read()["sessions"]["s1"]


def test_statusline_no_leave_without_a_target(tmp_path, monkeypatch):
    s = _leave_setup(tmp_path, monkeypatch, other=95)
    assert s["leave"] is None and s["leave_blocked"].startswith("none-eligible")


def test_statusline_leaves_when_another_account_is_free(tmp_path, monkeypatch):
    s = _leave_setup(tmp_path, monkeypatch, other=10)
    assert s["leave"] == "soft" and "leave_blocked" not in s


def _poll_status(tmp_path, monkeypatch, status, tok="tok1"):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    cfg = config.Config(config.Pool(), (config.Account("a", tmp_path / "a", 1),))
    monkeypatch.setattr(usage, "_oauth", lambda a: {"accessToken": tok})
    calls = []

    def boom(_):
        calls.append(1)
        raise usage.UsageError(status)

    monkeypatch.setattr(usage, "fetch", boom)
    return cfg, calls


def test_poll_403_marks_no_plan_and_repolls_only_after_relogin(tmp_path, monkeypatch):
    cfg, calls = _poll_status(tmp_path, monkeypatch, 403)
    usage.poll(cfg, 10_000)
    a = state.read()["accounts"]["a"]
    assert (a["auth"], a["poll_backoff_until"]) == ("no-plan", 10_600)
    assert len(a["token_fp"]) == 12 and "tok1" not in json.dumps(a)
    usage.poll(cfg, 10_100)
    assert len(calls) == 1
    monkeypatch.setattr(usage, "_oauth", lambda a: {"accessToken": "tok2"})
    usage.poll(cfg, 10_200)
    assert len(calls) == 2


def test_poll_401_marks_expired_without_backoff(tmp_path, monkeypatch):
    cfg, _ = _poll_status(tmp_path, monkeypatch, 401)
    usage.poll(cfg, 10_000)
    a = state.read()["accounts"]["a"]
    assert a["auth"] == "expired" and not a.get("poll_backoff_until")


def test_poll_429_backoff_holds_with_token(tmp_path, monkeypatch):
    cfg, calls = _poll_status(tmp_path, monkeypatch, 429)
    usage.poll(cfg, 10_000)
    usage.poll(cfg, 10_100)
    assert len(calls) == 1


def test_poll_429_after_403_and_relogin_keeps_doubling(tmp_path, monkeypatch):
    cfg, calls = _poll_status(tmp_path, monkeypatch, 403)
    usage.poll(cfg, 10_000)
    monkeypatch.setattr(usage, "_oauth", lambda a: {"accessToken": "tok2"})

    def rate_limited(_):
        calls.append(1)
        raise usage.UsageError(429)

    monkeypatch.setattr(usage, "fetch", rate_limited)
    usage.poll(cfg, 10_100)
    a = state.read()["accounts"]["a"]
    assert (a["poll_backoff_s"], a["poll_backoff_until"]) == (1200, 11_300)
    usage.poll(cfg, 10_200)
    assert len(calls) == 2
