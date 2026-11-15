"""Capacity-weighted pool outlook. Pure: no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from . import router
from .config import Config


@dataclass(frozen=True)
class Summary:
    room: float
    total: float
    room_ready: float
    burn: float | None
    runway_s: float | None
    next_at: int | None
    next_account: str | None
    next_gain: float | None
    seamless: bool
    gap_at: float | None
    gap_s: float | None
    week_used: float
    week_delta: float | None


def summary(cfg: Config, st: dict, now: int) -> Summary | None:
    accounts = st.get("accounts") or {}
    enabled = [a for a in cfg.accounts if a.enabled]
    if not any((accounts.get(a.name) or {}).get("updated_at") for a in enabled):
        return None
    caps = {a.name: router.capacity(accounts.get(a.name) or {}, a) for a in enabled}
    max_cap = max(caps.values())
    counted = {a.name: accounts.get(a.name) or {} for a in enabled
               if router.why_ineligible(accounts.get(a.name) or {}, now=now, pool=cfg.pool,
                                        ignore_cooldown=True) not in {"credits", "auth", "no-data"}}
    ratios = {name: caps[name] / max_cap for name in counted}
    room = ready = total = weekly = pace = pace_total = 0.0
    next_at = next_account = next_gain = None
    for name, acct in counted.items():
        ratio = ratios[name]
        used, reset = router.win(acct, "five_hour", now)
        available = (100 - used) / 100 * ratio
        room += available
        total += ratio
        if router.why_ineligible(acct, now=now, pool=cfg.pool) is None:
            ready += available
        if reset is not None and (next_at is None or reset < next_at):
            next_at, next_account, next_gain = reset, name, used / 100 * ratio
        used7, reset7 = router.win(acct, "seven_day", now)
        weekly += used7 * ratio
        if reset7 is not None:
            pace += min(1, max(0, 1 - (reset7 - now) / 604800)) * 100 * ratio
            pace_total += ratio
    burns = []
    for session in (st.get("sessions") or {}).values():
        name = session.get("account")
        if name not in counted or session.get("updated_at", 0) < now - 600:
            continue
        turns = session.get("turns") or []
        rates = [(b["u5h"] - a["u5h"]) / 100 / ((b["ts"] - a["ts"]) / 3600) * ratios[name]
                 for a, b in zip(turns, turns[1:])
                 if 0 < b["ts"] - a["ts"] <= 900 and b["u5h"] > a["u5h"]]
        if len(rates) >= 2:
            burns.append(median(rates))
    burn = sum(burns) if burns else None
    runway = room / burn * 3600 if burn else None
    seamless = runway is not None and next_at is not None and now + runway >= next_at
    gap_at = now + runway if runway is not None and next_at is not None and not seamless else None
    week_used = weekly / total if total else 0.0
    return Summary(room, total, ready, burn, runway, next_at, next_account, next_gain,
                   seamless, gap_at, next_at - gap_at if gap_at is not None else None,
                   week_used, week_used - pace / pace_total if pace_total else None)
