"""Best-effort JSONL event log. Never raises, never records credentials."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

from . import paths

_FORBIDDEN = frozenset({
    "token", "access_token", "refresh_token", "accesstoken", "refreshtoken",
    "secret", "password", "credentials", "authorization", "bearer",
})


def _secret_key(k) -> bool:
    k = str(k).lower()
    return k in _FORBIDDEN or k.endswith("_secret")


def _scrub(v):
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items() if not _secret_key(k)}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    return v


def log(kind: str, **fields) -> None:
    try:
        now = time.time()
        rec = {"ts": int(now), "kind": kind}
        for k, v in fields.items():
            if _secret_key(k):
                continue
            rec[k] = _scrub(v)
        d = paths.log_dir()
        d.mkdir(parents=True, exist_ok=True)
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        with open(d / f"{day}.jsonl", "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"cpool: journal error: {e}", file=sys.stderr)
