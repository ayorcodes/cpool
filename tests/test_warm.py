import subprocess
import time

from cpool import config, state, warm


def test_warm_returns_false_when_slot_claimed(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    with state.locked() as st:
        st["accounts"]["a"] = {"warm_started_at": int(time.time()) - 10}

    def fail(*a, **k):
        raise AssertionError("claude must not run")

    monkeypatch.setattr(subprocess, "run", fail)
    assert warm.warm(config.Account("a", tmp_path / "a", 1)) is False


def _warm_with(tmp_path, monkeypatch, run):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", run)
    ok = warm.warm(config.Account("a", tmp_path / "a", 1))
    return ok, state.read()["accounts"]["a"]


def test_warm_success_releases_claim(tmp_path, monkeypatch):
    ok, a = _warm_with(tmp_path, monkeypatch, lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert ok is True and "warmed_at" in a
    assert "warm_started_at" not in a and "warm_failed_at" not in a


def test_warm_timeout_releases_claim(tmp_path, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("claude", 1)

    ok, a = _warm_with(tmp_path, monkeypatch, slow)
    assert ok is False and "warm_failed_at" in a and "warm_started_at" not in a
