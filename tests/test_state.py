"""state.json is shared by concurrent writers (statusline, poller, wrapper);
a lost update would corrupt routing."""
import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest

PROCS, ITERS = 8, 25


def _worker(home: str) -> None:
    os.environ["CPOOL_HOME"] = home
    from cpool import state

    for _ in range(ITERS):
        with state.locked() as st:
            st["counter"] = st.get("counter", 0) + 1


def _save_worker(home: str, i: int) -> None:
    os.environ["CPOOL_HOME"] = home
    from cpool import config

    config.save_account(config.Account(f"a{i}", Path(home) / f"acct{i}", i + 1))


def test_concurrent_increments_not_lost(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    ctx = mp.get_context("spawn")
    ps = [ctx.Process(target=_worker, args=(str(tmp_path),)) for _ in range(PROCS)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
        assert p.exitcode == 0
    from cpool import state

    data = json.loads((tmp_path / "state.json").read_text())
    assert data["counter"] == PROCS * ITERS
    assert state.read()["counter"] == PROCS * ITERS


def test_raising_body_leaves_file_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    from cpool import state

    with state.locked() as st:
        st["counter"] = 1
    before = (tmp_path / "state.json").read_bytes()
    with pytest.raises(RuntimeError):
        with state.locked() as st:
            st["counter"] = 99
            raise RuntimeError("boom")
    assert (tmp_path / "state.json").read_bytes() == before


def test_concurrent_save_account_keeps_all_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    ctx = mp.get_context("spawn")
    ps = [ctx.Process(target=_save_worker, args=(str(tmp_path), i)) for i in range(PROCS)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
        assert p.exitcode == 0
    from cpool import config

    assert {a.name for a in config.load().accounts} == {f"a{i}" for i in range(PROCS)}


def test_wrong_shaped_state_does_not_block_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("CPOOL_HOME", str(tmp_path))
    from cpool import state

    path = tmp_path / "state.json"
    # wrong-shaped top-level container: file is moved aside as corrupt
    path.write_text('{"version":1,"accounts":[],"sessions":{}}')
    with state.locked() as st:
        st["counter"] = 1
    assert json.loads(path.read_text())["counter"] == 1
    assert list(tmp_path.glob("state.json.corrupt-*"))

    # non-object entries are dropped, valid ones kept
    path.write_text(
        json.dumps({"version": 1, "accounts": {"a": "oops", "b": {"x": 1}},
                    "sessions": {"x": "oops", "me": {"pid": os.getpid()}}})
    )
    with state.locked() as st:
        state.account_state(st, "a")["y"] = 2
    data = json.loads(path.read_text())
    assert data["accounts"] == {"a": {"y": 2}, "b": {"x": 1}}
    assert list(data["sessions"]) == ["me"]
