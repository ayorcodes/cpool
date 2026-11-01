"""state.json: shared, flock-guarded, atomically replaced."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager

from . import journal, paths


def _empty() -> dict:
    return {"version": 1, "accounts": {}, "sessions": {}}


def _load() -> dict:
    p = paths.state_path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return _empty()
    try:
        st = json.loads(text)
        if not isinstance(st, dict):
            raise ValueError("state root is not an object")
        for key in ("accounts", "sessions"):
            if key in st and not isinstance(st[key], dict):
                raise ValueError(f"state.{key} is not an object")
    except ValueError as e:
        dest = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
        try:
            os.replace(p, dest)
        except OSError:
            pass
        journal.log("error", where="state.load", error=str(e), moved_to=str(dest))
        return _empty()
    st.setdefault("version", 1)
    for key in ("accounts", "sessions"):
        entries = st.setdefault(key, {})
        for name in [n for n, v in entries.items() if not isinstance(v, dict)]:
            journal.log("error", where="state.load", error=f"dropped non-object {key} entry", entry=name)
            del entries[name]
    return st


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_pid_alive = pid_alive


def _prune(st: dict) -> None:
    sessions = st.get("sessions", {})
    if not isinstance(sessions, dict):
        return
    for sid in [s for s, v in sessions.items() if isinstance(v, dict) and not _pid_alive(v.get("pid"))]:
        del sessions[sid]


def _lock(shared: bool):
    paths.home().mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.state_path().with_name("state.json.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    return fd


def read() -> dict:
    fd = _lock(shared=True)
    try:
        return _load()
    finally:
        os.close(fd)


@contextmanager
def locked() -> Iterator[dict]:
    fd = _lock(shared=False)
    try:
        st = _load()
        yield st
        _prune(st)
        p = paths.state_path()
        tmp_fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".tmp.")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(st, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)


def _entry(st: dict, key: str, name: str) -> dict:
    entries = st.setdefault(key, {})
    if not isinstance(entries.get(name), dict):
        entries[name] = {}
    return entries[name]


def account_state(st: dict, name: str) -> dict:
    return _entry(st, "accounts", name)


def session_state(st: dict, sid: str) -> dict:
    return _entry(st, "sessions", sid)
