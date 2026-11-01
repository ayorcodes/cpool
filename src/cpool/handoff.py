"""Mod -> wrapper switch requests: ~/.cpool/handoff/<CPOOL_RUN>.json."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import journal, paths


@dataclass(frozen=True)
class Handoff:
    run: str
    session_id: str
    from_account: str
    model: str
    reason: str
    compacted: bool
    ts: int
    version: int = 1


def _path(run: str) -> Path:
    return paths.handoff_dir() / f"{run}.json"


def write(h: dict) -> Path:
    dest = _path(h["run"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".tmp.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(h, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def take(run: str) -> dict | None:
    p = _path(run)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        journal.log("error", where="handoff.take", run=run, error=str(e))
        return None
    try:
        h = json.loads(text)
        if not isinstance(h, dict) or h.get("version") != 1:
            raise ValueError("not a version-1 handoff object")
        for key in ("session_id", "from"):
            if not isinstance(h.get(key), str) or not h[key]:
                raise ValueError(f"missing {key}")
    except ValueError as e:
        journal.log("error", where="handoff.take", run=run, error=str(e))
        h = None
    try:
        p.unlink()
    except OSError:
        pass
    return h
