from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config

WINDOW_MIN = 300
SLOT_GRACE_MIN = 60
SESSION_LIVE_S = 120
WARM_RETRY_MIN = 15
WARM_INFLIGHT_S = 150
UNWARMABLE_AUTH = ("no-plan", "missing")


@dataclass(frozen=True)
class WarmSlot:
    account: str
    hour: int
    minute: int


def plan(cfg: Config) -> list[WarmSlot]:
    accts = sorted((a for a in cfg.accounts if a.enabled), key=lambda a: a.order)
    n = len(accts)
    if not n:
        return []
    k = max(30, (WINDOW_MIN // n) // 30 * 30)
    slots: list[WarmSlot] = []
    for i, a in enumerate(accts):
        reset = cfg.pool.start_hour * 60 + i * k
        first = reset - WINDOW_MIN + 2
        for j in range(4):
            m = (first + j * WINDOW_MIN) % 1440
            slots.append(WarmSlot(a.name, m // 60, m % 60))
    return slots


def due(cfg: Config, state: dict, now: datetime) -> list[str]:
    if now.tzinfo is not None:  # launchd fires in local time
        now = now.astimezone().replace(tzinfo=None)
    ts = now.timestamp()
    accounts = state.get("accounts") or {}
    sessions = state.get("sessions") or {}
    slots = plan(cfg)
    out: list[str] = []
    for a in sorted((a for a in cfg.accounts if a.enabled), key=lambda a: a.order):
        mine = [s for s in slots if s.account == a.name]
        if not any(_recent(s, now) for s in mine):
            continue
        st = accounts.get(a.name) or {}
        if st.get("auth") in UNWARMABLE_AUTH:
            continue
        started = st.get("warm_started_at")
        if started is not None and ts - started < WARM_INFLIGHT_S:
            continue
        resets = (st.get("five_hour") or {}).get("resets_at")
        if resets is not None and resets > ts:
            continue
        failed = st.get("warm_failed_at")
        if failed is not None and ts - failed < WARM_RETRY_MIN * 60:
            continue
        warmed = st.get("warmed_at")
        if warmed is not None and ts - warmed < SLOT_GRACE_MIN * 60:
            continue
        if any(
            (s or {}).get("account") == a.name
            and ts - ((s or {}).get("updated_at") or 0) < SESSION_LIVE_S
            for s in sessions.values()
        ):
            continue
        out.append(a.name)
    return out


def _recent(slot: WarmSlot, now: datetime) -> bool:
    for day in (0, 1):  # yesterday covers slots just before midnight
        at = (now - timedelta(days=day)).replace(
            hour=slot.hour, minute=slot.minute, second=0, microsecond=0
        )
        if timedelta(0) <= now - at <= timedelta(minutes=SLOT_GRACE_MIN):
            return True
    return False
