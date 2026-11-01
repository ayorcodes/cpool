from __future__ import annotations

import os
import random
import subprocess
import time
from datetime import datetime

from . import accounts, journal, paths, schedule, state
from .config import Account, Config

TIMEOUT_S = 120


def warm(acct: Account) -> bool:
    env = dict(os.environ)
    for k, v in accounts.launch_env(acct).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    try:
        rc = subprocess.run(
            ["claude", "-p", "--model", "haiku", "ok"],
            env=env,
            cwd=paths.home(),
            timeout=TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except (subprocess.TimeoutExpired, OSError):
        rc = -1
    ok = rc == 0
    with state.locked() as st:
        a = state.account_state(st, acct.name)
        if ok:
            a["warmed_at"] = int(time.time())
            a.pop("warm_failed_at", None)
        else:
            a["warm_failed_at"] = int(time.time())
    journal.log("warm", account=acct.name, ok=ok, rc=rc)
    return ok


def warm_due(cfg: Config, now: datetime | None = None) -> int:
    now = now or datetime.now()
    names = schedule.due(cfg, state.read(), now)
    by_name = {a.name: a for a in cfg.accounts}
    lo, hi = cfg.pool.jitter_s
    for i, name in enumerate(names):
        if i:
            time.sleep(random.uniform(lo, hi))
        warm(by_name[name])
    return len(names)
