"""Local rollout/auth decoding must not silently show usage from the wrong plan."""
import base64
import io
import json
import os

import pytest

from cpool import codex


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CPOOL_CODEX", raising=False)
    return tmp_path


def auth(home, plan):
    payload = {"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{token}.sig"}}))


def rollout(home, name, plan="plus", *, timestamp="2026-01-01T00:00:00Z", mtime=100):
    path = home / "sessions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    limits = {"plan_type": plan, "primary": {"window_minutes": 300, "used_percent": 25,
              "resets_at": 2000}, "secondary": {"window_minutes": 10080,
              "used_percent": 42, "resets_at": 9000}}
    path.write_text(json.dumps({"timestamp": timestamp, "payload": {"rate_limits": limits}},
                               separators=(",", ":")) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def test_plus_snapshot_decodes_both_windows(home):
    auth(home, "plus")
    rollout(home, "2026/01/01/rollout-plus.jsonl")
    assert codex.snapshot(1000) == {"plan": "plus", "windows": [(300, 25, 2000), (10080, 42, 9000)],
                                    "ts": 1767225600}


def test_current_plan_filters_newer_free_records(home):
    auth(home, "plus")
    rollout(home, "2026/01/01/rollout-plus.jsonl", mtime=100)
    rollout(home, "2026/01/02/rollout-free.jsonl", "free", mtime=200)
    assert codex.snapshot(1000)["plan"] == "plus"
    assert len(codex.snapshot(1000)["windows"]) == 2


def test_known_plan_without_match_keeps_unknown_windows(home):
    auth(home, "plus")
    rollout(home, "2026/01/01/rollout-free.jsonl", "free")
    assert codex.snapshot(1000) == {"plan": "plus", "windows": [], "ts": None}


def test_no_auth_uses_newest_record_and_skips_null(home):
    rollout(home, "2026/01/01/rollout-plus.jsonl", mtime=100)
    path = rollout(home, "2026/01/02/rollout-free.jsonl", "free", mtime=200)
    with path.open("a") as stream:
        stream.write('{"payload":{"rate_limits":null}}\n')
    assert codex.snapshot(1000)["plan"] == "free"


def test_full_rescan_finds_record_outside_tail(home):
    path = rollout(home, "2026/01/01/rollout-plus.jsonl")
    with path.open("a") as stream:
        stream.write('{"padding":"' + "x" * 70000 + '"}\n')
    assert codex.snapshot(1000)["windows"] == [(300, 25, 2000), (10080, 42, 9000)]


def test_missing_home_and_opt_out(home, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(home / "missing"))
    assert codex.snapshot(1000) is None
    monkeypatch.setenv("CODEX_HOME", str(home))
    auth(home, "plus")
    monkeypatch.setenv("CPOOL_CODEX", "off")
    assert codex.snapshot(1000) is None


def test_newest_days_across_calendar_boundaries(home):
    oldest = rollout(home, "2025/12/31/rollout-old.jsonl", mtime=900)
    january = rollout(home, "2026/01/31/rollout-january.jsonl", mtime=200)
    february = rollout(home, "2026/02/01/rollout-february.jsonl", mtime=300)
    newest = rollout(home, "2026/02/02/rollout-new.jsonl", mtime=400)
    assert codex.newest_rollouts(home, limit=3) == [newest, february, january]
    assert oldest not in codex.newest_rollouts(home, limit=3)


def test_fetch_usage_decodes_response_and_caches_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path / "cp"))
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "t"}}))
    body = {"plan_type": "plus", "rate_limit": {"primary_window": {
        "used_percent": 5, "limit_window_seconds": 18000, "reset_at": 99}}}
    monkeypatch.setattr(codex.urllib.request, "urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps(body).encode()))

    snap = codex.fetch_usage(tmp_path, None, 1000)
    assert snap["plan"] == "plus"
    assert snap["windows"] == [(300, 5.0, 99)]

    def boom(*a, **k):
        raise OSError("network unavailable")

    monkeypatch.setattr(codex.urllib.request, "urlopen", boom)
    assert codex.fetch_usage(tmp_path, None, 1010) == snap
