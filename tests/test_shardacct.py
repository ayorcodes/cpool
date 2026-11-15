"""Management auth and relay joins are security boundaries absent from existing tests."""
import hashlib
import io
import json
import os
import subprocess
from collections import Counter
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from cpool import config, shardacct, state


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(shardacct, "_rejected", set())
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    monkeypatch.setenv("CPOOL_SHARD_MGMT_KEY", "test-management-secret")
    monkeypatch.setattr(shardacct, "SHARD_AUTH", tmp_path / "relay.json")
    monkeypatch.setattr(shardacct.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1, "", "")))
    http = Mock(return_value=io.BytesIO(b'{"files":[{"auth_index":"abc123456","email":"USER@example.com"}]}'))
    monkeypatch.setattr(shardacct.urllib.request.OpenerDirector, "open", http)
    return tmp_path, http


def test_trace_join_and_all_fallbacks(sandbox, monkeypatch):
    root, http = sandbox
    account = root / "account"
    account.mkdir()
    (account / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"user@example.com"}}')
    cfg = config.Config(config.Pool(), (config.Account("primary", account, 1),))
    sessions = {sid: {"shard": True, "pid": os.getpid()} for sid in ("matched", "external", "unknown", "pending")}
    sessions["dead"] = {"shard": True, "pid": 0}
    sessions["normal"] = {"pid": os.getpid()}
    records = {sid: {"auth_index": index, "ts": 100} for sid, index in
               {"matched": "abc123456", "external": "other", "unknown": "unknown-index", "dead": "abc123456"}.items()}
    shardacct.SHARD_AUTH.write_text(json.dumps({"v": 1, "sessions": records}))
    http.return_value = io.BytesIO(json.dumps({"files": [
        {"auth_index": "abc123456", "email": "USER@example.com"},
        {"auth_index": "other", "email": "external@example.com"},
    ]}).encode())
    assert state.live_shard_ids({"sessions": sessions}) == {"matched", "external", "unknown", "pending"}
    assert state.live_shard_sessions({"sessions": sessions}) == 4
    assert shardacct.serving(cfg, {"sessions": sessions}, 100) == (
        Counter({"primary": 1, "external": 1, "#unknow": 1, "…": 1}), {"primary"})
    monkeypatch.delenv("CPOOL_SHARD_MGMT_KEY")
    assert shardacct.serving(cfg, {"sessions": sessions}, 101) == (
        Counter({"#abc123": 1, "#other": 1, "#unknow": 1, "…": 1}), set())


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_latches_across_cache_reads_until_changed(sandbox, monkeypatch, status):
    root, http = sandbox
    http.side_effect = HTTPError("http://localhost", status, "rejected", {}, None)
    assert shardacct.auth_emails(100) is None
    monkeypatch.setattr(shardacct, "_rejected", set())
    assert shardacct.auth_emails(10000) is None
    assert http.call_count == 1
    assert json.loads((root / "shard-auths.json").read_text())["error"] == "auth"
    monkeypatch.setenv("CPOOL_SHARD_MGMT_KEY", "changed-management-secret")
    assert shardacct.auth_emails(10001) is None
    assert http.call_count == 2


def test_alternating_rejected_keys_remain_latched_across_renders(sandbox, monkeypatch):
    root, http = sandbox
    http.side_effect = HTTPError("http://localhost", 401, "rejected", {}, None)
    keys = ["wrong-key-a", "wrong-key-b"]
    for render in range(8):
        monkeypatch.setattr(shardacct, "_rejected", set())
        monkeypatch.setenv("CPOOL_SHARD_MGMT_KEY", keys[render % 2])
        assert shardacct.auth_emails(100 + render) is None
    assert http.call_count == 2
    for index in range(7):
        keys.append(f"unseen-key-{index}")
        monkeypatch.setattr(shardacct, "_rejected", set())
        monkeypatch.setenv("CPOOL_SHARD_MGMT_KEY", keys[-1])
        assert shardacct.auth_emails(200 + index) is None
        assert http.call_count == 3 + index
    text = (root / "shard-auths.json").read_text()
    rejected = json.loads(text)["rejected_fps"]
    assert len(rejected) == 8
    assert set(rejected) == {hashlib.sha256(key.encode()).hexdigest()[:12] for key in keys[-8:]}
    assert all(key not in text for key in keys)
    monkeypatch.setattr(shardacct, "_rejected", set())
    monkeypatch.setenv("CPOOL_SHARD_MGMT_KEY", keys[0])
    assert shardacct.auth_emails(300) is None
    assert http.call_count == 10


@pytest.mark.parametrize("wrapper", [False, True])
def test_shapes_ttl_and_missing_index_throttle(sandbox, wrapper):
    root, http = sandbox
    entries = [{"auth_index": "abc123456", "email": "user@example.com"},
               {"auth_index": "label", "label": "label@example.com"},
               {"auth_index": "name", "name": "named"}]
    http.side_effect = lambda *a, **kw: io.BytesIO(json.dumps({"files": entries} if wrapper else entries).encode())
    expected = {"abc123456": "user@example.com", "label": "label@example.com", "name": "named"}
    assert shardacct.auth_emails(100) == expected
    assert shardacct.auth_emails(699) == expected
    assert http.call_count == 1
    assert shardacct.auth_emails(700) == expected
    assert http.call_count == 2
    shardacct.SHARD_AUTH.write_text(json.dumps({"v": 1, "sessions": {
        "dead": {"auth_index": "removed", "ts": 700},
        "normal": {"auth_index": "non-shard", "ts": 700},
        "absent": {"auth_index": "absent", "ts": 700},
        "s": {"auth_index": "new", "ts": 700},
    }}))
    with state.locked() as st:
        st["sessions"] = {"dead": {"shard": True, "pid": 0},
                          "normal": {"pid": os.getpid()}}
    assert shardacct.auth_emails(759) == expected
    assert http.call_count == 2
    assert shardacct.auth_emails(760) == expected
    assert http.call_count == 2
    with state.locked() as st:
        st["sessions"]["s"] = {"shard": True, "pid": os.getpid()}
    assert shardacct.auth_emails(761) == expected
    assert http.call_count == 3
    assert shardacct.auth_emails(762) == expected
    assert http.call_count == 3
    request = http.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8317/v0/management/auth-files"
    assert request.get_method() == "GET"
    assert http.call_args.kwargs["timeout"] == 1.0
    assert os.environ["CPOOL_SHARD_MGMT_KEY"] not in (root / "shard-auths.json").read_text()


def test_down_throttles(sandbox):
    root, http = sandbox
    http.side_effect = URLError("offline")
    assert shardacct.auth_emails(100) is None
    assert shardacct.auth_emails(159) is None
    assert http.call_count == 1
    assert shardacct.auth_emails(160) is None
    assert http.call_count == 2
    assert json.loads((root / "shard-auths.json").read_text())["error"] == "down"


def test_failed_cache_persistence_cannot_repeat_rejected_request(sandbox, monkeypatch):
    root, http = sandbox
    http.side_effect = HTTPError("http://localhost", 401, "rejected", {}, None)
    replace = shardacct.os.replace
    writes = 0

    def fail_second_write(source, target):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk unavailable")
        replace(source, target)

    monkeypatch.setattr(shardacct.os, "replace", fail_second_write)
    assert shardacct.auth_emails(100) is None
    assert shardacct.auth_emails(10000) is None
    assert http.call_count == 1
    assert json.loads((root / "shard-auths.json").read_text())["error"] == "auth"


@pytest.mark.parametrize("text", [None, "broken", "[]", '{"sessions":[]}', '{"sessions":{"s":null}}'])
def test_bad_relay_file_degrades_without_raising(sandbox, text):
    if text is not None:
        shardacct.SHARD_AUTH.write_text(text)
    assert shardacct.read_session_auth() == {}


def test_key_priority_and_stdin_storage(sandbox, monkeypatch):
    root, http = sandbox
    run = shardacct.subprocess.run
    assert shardacct.get_key() == os.environ["CPOOL_SHARD_MGMT_KEY"]
    run.assert_not_called()
    monkeypatch.delenv("CPOOL_SHARD_MGMT_KEY")
    run.return_value = subprocess.CompletedProcess([], 0, "keychain-secret\n", "")
    assert shardacct.get_key() == "keychain-secret"
    secret = 'special "key" with \\ escapes'
    shardacct.set_key(secret)
    assert run.call_args.args[0] == ["security", "-i"]
    assert secret not in str(run.call_args.args)
    assert "add-generic-password" in run.call_args.kwargs["input"]
    assert shardacct.clear_key() is True


def test_default_account_email(sandbox, monkeypatch):
    root, _ = sandbox
    monkeypatch.setattr(shardacct.Path, "home", lambda: root)
    (root / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"DEFAULT@EXAMPLE.COM"}}')
    cfg = config.Config(config.Pool(), (config.Account("default", None, 0),))
    assert shardacct.account_emails(cfg) == {"default@example.com": "default"}


@pytest.mark.parametrize("body", [b"", b"null", b'"invalid"', b"42", b"{}"])
def test_invalid_auth_response_takes_down_backoff(sandbox, body):
    root, http = sandbox
    http.side_effect = lambda *args, **kwargs: io.BytesIO(body)
    assert shardacct.auth_emails(100) is None
    assert shardacct.auth_emails(159) is None
    assert http.call_count == 1
    assert shardacct.auth_emails(160) is None
    assert http.call_count == 2
    cache = json.loads((root / "shard-auths.json").read_text())
    assert cache["error"] == "down"
    assert cache["map"] == {}
