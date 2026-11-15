"""Resolve relay-selected auth indices without exposing management credentials."""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import http.client
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypedDict

from . import config, paths, state

SERVICE = "cpool-shard-mgmt"
SHARD_AUTH = Path(os.environ.get("CPOOL_SHARD_AUTH_FILE", Path.home() / ".config" / "opclaude" / "shard-auth.json"))


class Cache(TypedDict):
    at: float
    key_fp: str
    rejected_fps: list[str]
    map: dict[str, str]
    error: Literal["auth", "down"] | None
    failures: int


_rejected: set[str] = set()


def get_key() -> str | None:
    if key := os.environ.get("CPOOL_SHARD_MGMT_KEY", "").strip():
        return key
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-w"],
                                capture_output=True, text=True)
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def set_key(key: str) -> None:
    if not key or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("invalid management key")
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    command = f'add-generic-password -U -s {SERVICE} -a {getpass.getuser()} -w "{escaped}"\n'
    try:
        result = subprocess.run(["security", "-i"], input=command, capture_output=True, text=True)
    except OSError:
        raise ValueError("could not write management key to Keychain") from None
    if result.returncode:
        raise ValueError("could not write management key to Keychain")


def clear_key() -> bool:
    try:
        return subprocess.run(["security", "delete-generic-password", "-s", SERVICE],
                              capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def prompt_key() -> None:
    set_key(getpass.getpass("cliproxyapi management key: ").strip())


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def read_session_auth() -> dict[str, str]:
    sessions = _read_json(SHARD_AUTH).get("sessions")
    if not isinstance(sessions, dict):
        return {}
    return {sid: entry["auth_index"] for sid, entry in sessions.items()
            if isinstance(entry, dict) and isinstance(entry.get("auth_index"), str) and entry["auth_index"]}


def cached() -> Cache:
    raw = _read_json(paths.home() / "shard-auths.json")
    at, fp, mapping, error, failures = (raw.get(k) for k in ("at", "key_fp", "map", "error", "failures"))
    rejected = raw.get("rejected_fps", [])
    rejected = list(dict.fromkeys(value for value in rejected if isinstance(value, str))) if isinstance(rejected, list) else []
    if error == "auth" and isinstance(fp, str) and fp not in rejected:
        rejected.append(fp)
    return Cache(at=at if isinstance(at, (int, float)) else 0,
                 key_fp=fp if isinstance(fp, str) else "",
                 rejected_fps=rejected[-8:],
                 map={k: v for k, v in mapping.items() if isinstance(v, str)} if isinstance(mapping, dict) else {},
                 error=error if error in ("auth", "down") else None,
                 failures=failures if isinstance(failures, int) else 0)


def _save(cache: Cache) -> None:
    path = paths.home() / "shard-auths.json"
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(cache, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _fetch(key: str, now: float, previous: Cache) -> Cache:
    fp = hashlib.sha256(key.encode()).hexdigest()[:12]
    failures = previous["failures"] if previous["key_fp"] == fp else 0
    rejected = [value for value in previous["rejected_fps"] if value != fp]
    result = Cache(at=now, key_fp=fp, rejected_fps=(rejected + [fp])[-8:],
                   map={}, error="auth", failures=failures)
    # Persist a fail-closed latch before sending: a crash or failed cache write cannot trigger retries.
    _save(result)
    _rejected.intersection_update(result["rejected_fps"])
    _rejected.add(fp)
    try:
        base = config.load().shard.url.rstrip("/").removesuffix("/v1")
        request = urllib.request.Request(base + "/v0/management/auth-files",
                                         headers={"Authorization": f"Bearer {key}"})
        # Redirects must not forward the management credential to another endpoint.
        opener = urllib.request.OpenerDirector()
        opener.add_handler(urllib.request.HTTPHandler())
        opener.add_handler(urllib.request.HTTPSHandler())
        opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        with opener.open(request, timeout=1.0) as response:
            raw = json.load(response)
        entries = raw if isinstance(raw, list) else next(
            (value for value in raw.values() if isinstance(value, list)), []) if isinstance(raw, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            index = entry.get("auth_index")
            label = entry.get("email") or entry.get("label") or entry.get("name")
            if isinstance(index, str) and index and isinstance(label, str) and label:
                result["map"][index] = label
        result["error"] = None
        result["failures"] = 0
    except urllib.error.HTTPError as error:
        result["error"] = "auth" if error.code in (401, 403) else "down"
        result["failures"] += error.code in (401, 403)
    except (OSError, ValueError, http.client.HTTPException):
        result["error"] = "down"
    if result["error"] != "auth":
        result["rejected_fps"] = rejected[-8:]
    _save(result)
    if result["error"] != "auth":
        _rejected.discard(fp)
    return result


def auth_emails(now: float) -> dict[str, str] | None:
    key = get_key()
    if not key:
        return None
    fp = hashlib.sha256(key.encode()).hexdigest()[:12]
    try:
        paths.home().mkdir(parents=True, exist_ok=True)
        with (paths.home() / "shard-auths.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cache = cached()
            if fp in _rejected or fp in cache["rejected_fps"]:
                return None
            age = now - cache["at"]
            indices = read_session_auth()
            missing = {indices[sid] for sid in state.live_shard_ids(state.read()) if sid in indices} - cache["map"].keys()
            if cache["key_fp"] != fp or (age >= (60 if cache["error"] else 600)) or (missing and age >= 60):
                cache = _fetch(key, now, cache)
            return cache["map"] if cache["error"] is None else None
    except (OSError, ValueError):
        return None


def check_auth(now: float) -> Cache | None:
    key = get_key()
    if not key:
        return None
    try:
        paths.home().mkdir(parents=True, exist_ok=True)
        with (paths.home() / "shard-auths.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _fetch(key, now, cached())
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=32)
def account_emails(cfg: config.Config) -> dict[str, str]:
    emails = {}
    for account in cfg.accounts:
        path = account.dir / ".claude.json" if account.dir is not None else Path.home() / ".claude.json"
        oauth = _read_json(path).get("oauthAccount")
        email = oauth.get("emailAddress") if isinstance(oauth, dict) else None
        if isinstance(email, str) and email:
            emails[email.lower()] = account.name
    return emails


def live_by_account(cfg: config.Config, st: dict, now: float) -> tuple[Counter[str], Counter[str]]:
    live = {}
    seen = set()
    sessions = sorted((st.get("sessions") or {}).items(),
                      key=lambda item: item[1].get("updated_at") or 0, reverse=True)
    per_account: Counter[str] = Counter()
    for sid, session in sessions:
        pid = session.get("pid")
        if not state.pid_alive(pid) or pid in seen:
            continue
        seen.add(pid)
        if session.get("shard") is True:
            live[sid] = session
        elif name := session.get("account"):
            per_account[name] += 1
    counts, matched = serving(cfg, {**st, "sessions": live}, now)
    unmatched: Counter[str] = Counter()
    for label, count in counts.items():
        if label in matched:
            per_account[label] += count
        else:
            unmatched[label] += count
    return per_account, unmatched


def serving(cfg: config.Config, st: dict, now: float) -> tuple[Counter[str], set[str]]:
    sessions = state.live_shard_ids(st)
    counts: Counter[str] = Counter()
    matched: set[str] = set()
    if not sessions:
        return counts, matched
    indices = read_session_auth()
    emails = auth_emails(now) if any(sid in indices for sid in sessions) else None
    accounts = account_emails(cfg)
    for sid in sessions:
        index = indices.get(sid)
        email = (emails or {}).get(index) if index else None
        name = accounts.get(email.lower()) if email else None
        label = name or email.split("@", 1)[0] if email else f"#{index[:6]}" if index else "…"
        counts[label] += 1
        if name:
            matched.add(name)
    return counts, matched
