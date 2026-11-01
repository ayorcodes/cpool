"""Statusline wrapper: payload on stdin -> state.json, then chain to the user's own statusline."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import accounts, config, journal, state


def _ancestor_claude_pid() -> int:
    pid = os.getpid()
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.split(None, 1)
        except (OSError, subprocess.SubprocessError):
            break
        if len(out) < 2:
            break
        ppid, comm = int(out[0]), out[1].strip()
        if pid != os.getpid() and comm.endswith("claude"):
            return pid
        if ppid <= 1:
            break
        pid = ppid
    return os.getppid()


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _window(payload: dict, key: str) -> dict | None:
    w = (payload.get("rate_limits") or {}).get(key)
    if not isinstance(w, dict) or _num(w.get("used_percentage")) is None:
        return None
    r = _num(w.get("resets_at"))
    return {"used": _num(w["used_percentage"]), "resets_at": int(r) if r is not None else None}


def _fresher(stored, new: dict | None, now: int) -> bool:
    """A payload window may replace the stored one unless it is from an expired or older window
    (an idle session re-rendering its last payload)."""
    if new is None:
        return False
    r = new["resets_at"]
    old = stored.get("resets_at") if isinstance(stored, dict) else None
    if r is None:
        return not (isinstance(old, (int, float)) and old > now)
    if r <= now:
        return False
    return not (isinstance(old, (int, float)) and old > r)


def _record(payload: dict) -> str | None:
    cfg = config.load()
    acct = accounts.resolve_account(cfg, os.environ.get("CLAUDE_CONFIG_DIR"))
    if acct is None:
        return None
    now = int(time.time())
    five, seven = _window(payload, "five_hour"), _window(payload, "seven_day")
    sid = payload.get("session_id")
    model = (payload.get("model") or {}).get("id")
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd")
    ctx = _num((payload.get("context_window") or {}).get("used_percentage"))
    cache = payload.get("prompt_cache")
    pid = _ancestor_claude_pid() if isinstance(sid, str) and sid else None
    with state.locked() as st:
        a = state.account_state(st, acct.name)
        five = five if _fresher(a.get("five_hour"), five, now) else None
        seven = seven if _fresher(a.get("seven_day"), seven, now) else None
        if five:
            a["five_hour"] = five
        if seven:
            a["seven_day"] = seven
        if five or seven:
            a.update(source="statusline", updated_at=now, auth="ok")
        if isinstance(sid, str) and sid:
            s = state.session_state(st, sid)
            if s.get("account") not in (None, acct.name):
                s["leave_manual"] = False  # a manual switch is spent once the session is on another account
            s.update(account=acct.name, run=os.environ.get("CPOOL_RUN"), pid=pid, cwd=cwd,
                     model=model, updated_at=now, ctx_used=ctx, cache=cache if isinstance(cache, dict) else {})
            s.setdefault("started_at", now)
            turns = s.get("turns") if isinstance(s.get("turns"), list) else []
            if five and (not turns or turns[-1].get("u5h") != five["used"]):
                turns.append({"ts": now, "u5h": five["used"]})
            s["turns"] = turns[-6:]
            try:
                from . import router
                s["leave"] = router.should_leave(s, a, cfg.pool, now)
            except (ImportError, NotImplementedError):
                s.pop("leave", None)
    return acct.name


def _compact(payload: dict, name: str | None) -> str:
    def pct(key):
        w = _window(payload, key)
        return f"{w['used']:.0f}%" if w else "?"
    return f"cpool:{name or '?'} 5h {pct('five_hour')} 7d {pct('seven_day')}"


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}
    name = None
    try:
        name = _record(payload)
    except Exception as e:  # noqa: BLE001
        journal.log("error", where="statusline", error=type(e).__name__, detail=str(e)[:200])
    nxt = os.environ.get("CPOOL_STATUSLINE_NEXT")
    if nxt:
        try:
            r = subprocess.run(["/bin/sh", "-c", nxt], input=raw, capture_output=True, timeout=2)
            sys.stdout.write(r.stdout.decode(errors="replace"))
            return 0
        except (OSError, subprocess.SubprocessError) as e:
            journal.log("error", where="statusline.chain", error=type(e).__name__)
    print(_compact(payload, name))
    return 0
