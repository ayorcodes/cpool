"""Account routing. Pure: no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .config import Config, Pool

FAMILIES = ("opus", "sonnet", "haiku", "fable")


@dataclass(frozen=True)
class Decision:
    account: str | None
    scores: dict[str, float]
    reason: str
    earliest_reset: int | None


def win(acct: dict, key: str, now: int) -> tuple[float, int | None]:
    """(used, resets_at) of a window; an expired or missing window reads as empty."""
    w = acct.get(key) or {}
    resets_at = w.get("resets_at")
    if resets_at is None or resets_at <= now:
        return 0.0, None
    return float(w.get("used") or 0.0), resets_at


def family(model: str | None) -> str | None:
    m = (model or "").lower()
    for f in FAMILIES:
        if f in m:
            return f
    return None


def capacity(acct_state: dict, cfg_acct) -> float:
    cap = getattr(cfg_acct, "capacity", None)
    if cap:
        return float(cap)
    tier = acct_state.get("tier") or ""
    if "max_20x" in tier:
        return 20.0
    if "max_5x" in tier:
        return 5.0
    return 1.0


def _model_used(acct: dict, fam: str | None, now: int) -> float:
    if fam is None:
        return 0.0
    return win(acct.get("model_week") or {}, fam, now)[0]


def _model_limited(acct: dict, fam: str | None, now: int) -> bool:
    lim = acct.get("limited") or {}
    until = lim.get("until")
    if until is None or until <= now:
        return False
    m = lim.get("model")
    return m == "all" or (fam is not None and m == fam)


def score(
    name: str,
    acct: dict,
    sessions: dict,
    *,
    cwd: str,
    now: int,
    pool: Pool,
    model: str | None = None,
    cap_ratio: float = 1.0,
) -> float | None:
    """None = ineligible. cap_ratio is capacity / max capacity in the pool."""
    if acct.get("credits_enabled") is not False:
        return None
    if acct.get("auth") not in (None, "ok"):
        return None
    updated_at = acct.get("updated_at")
    if not updated_at:
        return None
    u5h, reset5 = win(acct, "five_hour", now)
    u7d, _ = win(acct, "seven_day", now)
    if u5h >= pool.soft or u7d >= pool.weekly_soft:
        return None
    fam = family(model)
    if _model_used(acct, fam, now) >= pool.weekly_soft or _model_limited(acct, fam, now):
        return None
    left_at = acct.get("left_at")
    if left_at is not None and now - left_at < pool.cooldown_min * 60:
        return None

    s = (0.7 * (100 - u5h) + 0.25 * (100 - u7d)) * cap_ratio
    if reset5 is not None and reset5 - now <= 90 * 60 and u5h < 60:
        s += 15
    mine = [x for x in sessions.values() if x.get("account") == name]
    if any(x.get("cwd") == cwd for x in mine):
        s += 10
    s -= 3 * len(mine)
    if now - updated_at > pool.stale_min * 60:
        s -= 20
    return s


def _earliest_reset(state_accts: dict, names: list[str], now: int) -> int | None:
    for key in ("five_hour", "seven_day"):
        rs = [r for n in names if (r := win(state_accts.get(n) or {}, key, now)[1]) is not None]
        if rs:
            return min(rs)
    return None


def choose(
    state: dict,
    cfg: Config,
    *,
    model: str,
    cwd: str,
    now: int,
    current: str | None = None,
    exclude: frozenset[str] = frozenset(),
    forced: bool = False,
) -> Decision:
    enabled = [a for a in cfg.accounts if a.enabled]
    state_accts = state.get("accounts") or {}
    sessions = state.get("sessions") or {}
    excluded = set(exclude) | ({current} if forced and current else set())
    caps = {a.name: capacity(state_accts.get(a.name) or {}, a) for a in enabled}
    max_cap = max(caps.values(), default=1.0)

    scores: dict[str, float] = {}
    for a in enabled:
        if a.name in excluded:
            continue
        s = score(
            a.name, state_accts.get(a.name) or {}, sessions,
            cwd=cwd, now=now, pool=cfg.pool, model=model, cap_ratio=caps[a.name] / max_cap,
        )
        if s is not None:
            scores[a.name] = s

    if not scores:
        names = [a.name for a in enabled if a.name not in excluded]
        reason = "none-eligible: " + (", ".join(names) if names else "no accounts")
        return Decision(None, {}, reason, _earliest_reset(state_accts, names, now))

    best = max(scores, key=lambda n: scores[n])
    if current in scores and not forced:
        if best == current:
            return Decision(current, scores, "best", None)
        if scores[best] - scores[current] >= cfg.pool.margin:
            return Decision(best, scores, "margin", None)
        return Decision(current, scores, "sticky", None)
    return Decision(best, scores, "best", None)


def should_leave(sess: dict, acct: dict, pool: Pool, now: int) -> str | None:
    u5h, _ = win(acct, "five_hour", now)
    if u5h >= pool.soft:
        return "soft"
    vals = [t["u5h"] for t in (sess.get("turns") or [])[-6:] if t.get("u5h") is not None]
    deltas = [b - a for a, b in zip(vals, vals[1:]) if b - a > 0]
    est = median(deltas) if len(deltas) >= 2 else 0.0
    if u5h + est >= pool.projected:
        return "projected"
    fam = family(sess.get("model"))
    if _model_used(acct, fam, now) >= pool.weekly_soft or _model_limited(acct, fam, now):
        return "model"
    return None
