"""Read-only usage poller. The Keychain access token is read, used for one GET, and dropped:
never logged, written, refreshed or put in an exception message."""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime

from . import __version__, accounts, journal, paths, state
from .config import Account, Config

URL = "https://api.anthropic.com/api/oauth/usage"
FRESH_S = 120
BACKOFF_MAX_S = 7200


class UsageError(Exception):
    def __init__(self, status: int | None = None, message: str = "", *, retry_after: float = 0) -> None:
        super().__init__(message or f"usage error (status {status})")
        self.status = status
        self.retry_after = retry_after


class CredentialsMissing(UsageError):
    def __init__(self) -> None:
        super().__init__(None, "no credentials")


def _oauth(acct: Account) -> dict | None:
    service = accounts.keychain_service(accounts.launch_env(acct)["CLAUDE_CONFIG_DIR"])
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    try:
        oauth = json.loads(r.stdout)["claudeAiOauth"]
    except (ValueError, KeyError, TypeError):
        return None
    return oauth if isinstance(oauth, dict) else None


def token(acct: Account) -> str | None:
    oauth = _oauth(acct)
    tok = oauth.get("accessToken") if oauth else None
    return tok if isinstance(tok, str) and tok else None


def _subscription(oauth: dict | None) -> bool | None:
    if oauth is None or "subscriptionType" not in oauth:
        return None
    subscription = oauth["subscriptionType"]
    return isinstance(subscription, str) and bool(subscription.strip()) and subscription.strip().lower() != "free"


def has_subscription(acct: Account) -> bool | None:
    return _subscription(_oauth(acct))


def tier(acct: Account) -> str | None:
    oauth = _oauth(acct)
    t = oauth.get("rateLimitTier") if oauth else None
    return t if isinstance(t, str) else None


def _epoch(iso) -> int | None:
    if not isinstance(iso, str):
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


def _window(w) -> dict | None:
    if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
        return None
    return {"used": float(w["utilization"]), "resets_at": _epoch(w.get("resets_at"))}


def normalize(body: dict, now: int, tier: str | None = None) -> dict:
    out: dict = {}
    for src, dst in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
        w = _window(body.get(src))
        if w:
            out[dst] = w
    model_week = {}
    for src, name in (("seven_day_opus", "opus"), ("seven_day_sonnet", "sonnet")):
        w = _window(body.get(src))
        if w:
            model_week[name] = w
    out["model_week"] = model_week
    eu, spend = body.get("extra_usage"), body.get("spend")
    if isinstance(eu, dict) or isinstance(spend, dict):
        out["credits_enabled"] = bool((eu or {}).get("is_enabled")) or bool((spend or {}).get("enabled"))
    else:
        out["credits_enabled"] = None
    limits = body.get("limits")
    out["limits"] = limits if isinstance(limits, list) else []
    out["tier"] = tier
    out.update(source="poll", updated_at=now, auth="ok")
    return out


def fetch(acct: Account) -> dict:
    oauth = _oauth(acct)
    if _subscription(oauth) is False:
        raise UsageError(403, "no subscription")
    tok = oauth.get("accessToken") if oauth else None
    if not isinstance(tok, str) or not tok:
        raise CredentialsMissing()
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)) and exp / 1000 <= time.time():
        raise UsageError(401, "token expired")
    t = oauth.get("rateLimitTier")
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"cpool/{__version__}",
    })
    del tok, oauth
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        retry_after = 0
        header = e.headers.get("Retry-After") if e.code == 429 and e.headers else None
        if header:
            try:
                retry_after = max(0, int(header))
            except ValueError:
                try:
                    retry_after = max(0, parsedate_to_datetime(header).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        raise UsageError(e.code, retry_after=retry_after) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        raise UsageError(None, "network error") from None
    except ValueError:
        raise UsageError(None, "bad response") from None
    if not isinstance(body, dict):
        raise UsageError(None, "bad response")
    return normalize(body, int(time.time()), t if isinstance(t, str) else None)


def _fingerprint(acct: Account) -> str | None:
    tok = token(acct)
    return hashlib.sha256(tok.encode()).hexdigest()[:12] if tok else None


def _backoff(a: dict, now: int, start_s: int) -> None:
    prev = a.get("poll_backoff_s")
    step = min(prev * 2, BACKOFF_MAX_S) if isinstance(prev, (int, float)) and prev else start_s
    a["poll_backoff_s"] = step
    a["poll_backoff_until"] = now + step


def _skip(st: dict, acct: Account, now: int) -> str | None:
    name = acct.name
    a = st.get("accounts", {}).get(name, {})
    until = a.get("poll_backoff_until")
    if isinstance(until, (int, float)) and until > now:
        fp = a.get("token_fp")
        if fp is None or fp == _fingerprint(acct):
            return "backoff"
    ts = a.get("updated_at")
    if a.get("source") == "statusline" and isinstance(ts, (int, float)) and now - ts < FRESH_S:
        return "live"
    return None


def poll_due(cfg: Config, st: dict, now: int) -> list[str]:
    if cfg.pool.live_poll_s == 0:
        return []
    due = []
    for acct in cfg.accounts:
        a = st.get("accounts", {}).get(acct.name, {})
        if not acct.enabled or a.get("auth") in {"missing", "expired", "no-plan"}:
            continue
        if (a.get("poll_backoff_until") or 0) > now:
            continue
        ts = a.get("updated_at")
        if ts is None or now - ts >= cfg.pool.live_poll_s:
            due.append(acct.name)
    return due


def kick(cfg: Config, now: int) -> bool:
    if cfg.pool.live_poll_s == 0:
        return False
    from .run import _cpool_bin

    try:
        paths.home().mkdir(parents=True, exist_ok=True)
        with (paths.home() / "poll.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stamp = paths.home() / "poll.kicked"
            try:
                previous = float(stamp.read_text())
            except (FileNotFoundError, ValueError):
                previous = None
            if previous is not None and 0 <= now - previous < cfg.pool.live_poll_s:
                return False
            stamp.write_text(str(now))
            subprocess.Popen([*shlex.split(_cpool_bin()), "poll", "--due"],
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except (OSError, ValueError):
        return False


def poll(cfg: Config, now: int, *, due: bool = False) -> None:
    targets = set(poll_due(cfg, state.read(), now)) if due else None
    for acct in cfg.accounts:
        if not acct.enabled:
            continue
        if targets is not None:
            if acct.name not in targets:
                continue
        elif _skip(state.read(), acct, now):
            continue
        result, err = None, None
        try:
            result = fetch(acct)
        except UsageError as e:
            err = e
        fp = _fingerprint(acct) if err is not None and err.status == 403 else None
        with state.locked() as st:
            a = state.account_state(st, acct.name)
            if result is not None:
                result["updated_at"] = now
                a.update(result)
                a["poll_backoff_until"] = None
                a["poll_backoff_s"] = None
                a["token_fp"] = None
                journal.log("poll", account=acct.name, status=200,
                            five_hour=result.get("five_hour", {}).get("used"),
                            seven_day=result.get("seven_day", {}).get("used"))
                continue
            status = err.status
            if status == 401:
                a["auth"] = "expired"
            elif status == 403:
                a["auth"] = "no-plan"
                _backoff(a, now, cfg.pool.poll_min * 60)
                a["token_fp"] = fp
            elif isinstance(err, CredentialsMissing):
                a["auth"] = "missing"
            elif status == 429:
                _backoff(a, now, cfg.pool.poll_min * 60)
                a["poll_backoff_s"] = max(err.retry_after, a["poll_backoff_s"])
                a["poll_backoff_until"] = now + a["poll_backoff_s"]
                a["token_fp"] = None
            journal.log("poll", account=acct.name, status=status, error=str(err))


def poll_daemon(cfg: Config) -> int:
    try:
        while True:
            try:
                poll(cfg, int(time.time()))
            except Exception as e:  # noqa: BLE001
                journal.log("error", where="usage.poll_daemon", error=type(e).__name__)
            time.sleep(cfg.pool.poll_min * 60)
    except KeyboardInterrupt:
        return 0
