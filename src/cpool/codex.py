"""Read-only snapshots of Codex's local rate-limit records."""
from __future__ import annotations

import base64
import json
import math
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from . import paths

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_TTL = 60


class Snapshot(TypedDict):
    plan: str | None
    windows: list[tuple[int, float, int | None]]
    ts: float | None


def codex_home() -> Path:
    return Path(os.environ["CODEX_HOME"]) if "CODEX_HOME" in os.environ else Path.home() / ".codex"


def current_plan(home: Path) -> str | None:
    try:
        auth = json.loads((home / "auth.json").read_text())
        payload = auth["tokens"]["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        plan = claims["https://api.openai.com/auth"]["chatgpt_plan_type"]
        return plan if isinstance(plan, str) and plan else None
    except Exception:
        return None


def newest_rollouts(home: Path, limit: int = 5) -> list[Path]:
    files = []
    if limit <= 0 or not (home / "sessions").is_dir():
        return files
    for year in sorted((home / "sessions").iterdir(), reverse=True):
        if not year.is_dir() or not year.name.isdigit():
            continue
        for month in sorted(year.iterdir(), reverse=True):
            if not month.is_dir() or not month.name.isdigit():
                continue
            for day in sorted(month.iterdir(), reverse=True):
                if not day.is_dir() or not day.name.isdigit():
                    continue
                files.extend(p for p in day.glob("rollout-*.jsonl") if p.is_file())
                if len(files) >= limit:
                    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def last_rate_limits(path: Path, plan: str | None) -> Snapshot | None:
    size = path.stat().st_size
    offset = max(0, size - 65536)
    with path.open("rb") as stream:
        stream.seek(offset)
        if offset:
            stream.readline()
        lines = stream.readlines()
        for attempt in range(2):
            for line in reversed(lines):
                if b'"rate_limits":{' not in line:
                    continue
                try:
                    record = json.loads(line)
                    limits = record["payload"]["rate_limits"]
                    record_plan = limits.get("plan_type")
                    if record_plan is not None and not isinstance(record_plan, str):
                        continue
                    if plan is not None and record_plan != plan:
                        continue
                    windows = []
                    for key in ("primary", "secondary"):
                        window = limits.get(key)
                        if window is None:
                            continue
                        minutes = window["window_minutes"]
                        used = window["used_percent"]
                        reset = window.get("resets_at")
                        if (type(minutes) is not int or minutes <= 0
                                or type(used) not in (int, float) or not math.isfinite(used)
                                or (reset is not None and type(reset) is not int)):
                            raise ValueError("Invalid rate-limit window")
                        windows.append((minutes, float(used), reset))
                    ts = path.stat().st_mtime
                    try:
                        ts = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
                    except (KeyError, TypeError, ValueError, AttributeError):
                        pass
                    return {"plan": record_plan, "windows": windows, "ts": ts}
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
            if attempt or not offset or size > 2 * 1024 * 1024:
                break
            stream.seek(0)
            lines = stream.readlines()
    return None


def fetch_usage(home: Path, plan: str | None, now: int) -> Snapshot | None:
    """Rate limits from the ChatGPT usage endpoint, for when rollouts carry none (e.g. a proxy provider).

    Cached for USAGE_TTL seconds, failures included, so the statusline never hammers the network.
    """
    cache = paths.home() / "codex-usage.json"
    try:
        cached = json.loads(cache.read_text())
        if now - cached["at"] < USAGE_TTL:
            snap = cached["snap"]
            if snap:
                snap["windows"] = [tuple(w) for w in snap["windows"]]
            return snap
    except Exception:
        pass
    snap: Snapshot | None = None
    try:
        tokens = json.loads((home / "auth.json").read_text())["tokens"]
        headers = {"Authorization": f"Bearer {tokens['access_token']}", "User-Agent": "cpool"}
        if tokens.get("account_id"):
            headers["ChatGPT-Account-Id"] = tokens["account_id"]
        with urllib.request.urlopen(urllib.request.Request(USAGE_URL, headers=headers), timeout=2) as r:
            body = json.load(r)
        windows = []
        for key in ("primary_window", "secondary_window"):
            w = (body.get("rate_limit") or {}).get(key)
            if w:
                windows.append((int(w["limit_window_seconds"]) // 60, float(w["used_percent"]), w.get("reset_at")))
        if windows:
            snap = {"plan": body.get("plan_type") or plan, "windows": windows, "ts": float(now)}
    except Exception:
        snap = None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"at": now, "snap": snap}))
    except OSError:
        pass
    return snap


def snapshot(now: int) -> Snapshot | None:
    try:
        if os.environ.get("CPOOL_CODEX") == "off":
            return None
        home = codex_home()
        if not home.is_dir():
            return None
        plan = current_plan(home)
        for path in newest_rollouts(home):
            result = last_rate_limits(path, plan)
            if result is not None:
                return result
        fetched = None if os.environ.get("CPOOL_CODEX_USAGE") == "off" else fetch_usage(home, plan, now)
        if fetched is not None:
            return fetched
        return {"plan": plan, "windows": [], "ts": None} if plan is not None else None
    except Exception:
        return None
