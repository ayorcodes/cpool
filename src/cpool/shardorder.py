"""Synchronize fill-first account priorities with the shard management API."""
from __future__ import annotations

import json
import math
import os
import tempfile
from typing import TypedDict

from . import journal, paths, router, shardacct, state
from .config import Config


class SyncState(TypedDict):
    key_fp: str
    priorities: dict[str, int]
    rejected: bool


def order(st: dict, cfg: Config, now: int) -> list[str]:
    enabled = [a for a in cfg.accounts if a.enabled]
    accts = st.get("accounts") or {}
    caps = {a.name: router.capacity(accts.get(a.name) or {}, a) for a in enabled}
    max_cap = max(caps.values(), default=1.0)
    eligible = {}
    ineligible = []
    for a in enabled:
        acct = accts.get(a.name) or {}
        if router.why_ineligible(acct, now=now, pool=cfg.pool, ignore_cooldown=True) is not None:
            ineligible.append(a.name)
            continue
        score = router.score(a.name, acct, {}, cwd="", now=now, pool=cfg.pool,
                             cap_ratio=caps[a.name] / max_cap, ignore_cooldown=True)
        reset = (acct.get("five_hour") or {}).get("resets_at")
        eligible[a.name] = (reset if reset and reset > now else math.inf, -score)
    return sorted(eligible, key=eligible.__getitem__) + ineligible


def _save(saved: SyncState) -> None:
    path = paths.home() / "shard-order.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".shard-order-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(saved, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def sync(cfg: Config, now: int) -> bool:
    stage = "key"
    saved = None
    fp = None
    try:
        if not cfg.shard.enabled or not (key := shardacct.get_key()):
            return False
        fp = shardacct.key_fp(key)
        stage = "read"
        path = paths.home() / "shard-order.json"
        if path.exists():
            try:
                saved = json.loads(path.read_text())
            except ValueError:
                saved = None
            if not isinstance(saved, dict) or not isinstance(saved.get("key_fp"), str) or not isinstance(
                saved.get("priorities"), dict
            ) or not isinstance(saved.get("rejected"), bool):
                saved = None
            if saved and saved["key_fp"] != fp:
                saved = None
        if saved and saved["rejected"]:
            return False
        stage = "fetch"
        raw = shardacct.mgmt("/v0/management/auth-files", key)
        entries = shardacct.entries(raw)
        emails = shardacct.account_emails(cfg)
        rank = order(state.read(), cfg, now)
        priorities = {name: 100 - i for i, name in enumerate(rank)}
        desired = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "claude":
                continue
            email, name = entry.get("email"), entry.get("name")
            if not isinstance(email, str) or not isinstance(name, str) or not name:
                continue
            account = emails.get(email.lower())
            if account in priorities:
                desired[name] = priorities[account]
        previous = saved["priorities"] if saved else {}
        if saved and desired == previous:
            return False
        stage = "patch"
        patched = False
        for name, priority in desired.items():
            if previous.get(name) == priority:
                continue
            shardacct.mgmt("/v0/management/auth-files/fields", key, method="PATCH",
                           body={"name": name, "priority": priority})
            patched = True
        stage = "save"
        _save(SyncState(key_fp=fp, priorities=desired, rejected=False))
        journal.log("shard-order", order=rank, priorities=desired)
        return patched
    except Exception as e:
        status = getattr(e, "code", None)
        journal.log("shard-order-error", stage=stage, error=type(e).__name__, status=status)
        if stage in ("fetch", "patch") and status in (401, 403):
            try:
                _save(SyncState(key_fp=fp, priorities=saved["priorities"] if saved else {}, rejected=True))
            except Exception as save_error:
                journal.log("shard-order-error", stage="save", error=type(save_error).__name__, status=None)
        return False
