"""Shared account usage display, independent of terminal and state I/O."""
from __future__ import annotations

import math
import os
import time
from typing import Literal, TextIO

from . import chain, router, shardacct, state
from .config import Config
from .codex import Snapshot

FULL = "▰"
EMPTY = "▱"
ACTIVE = "▸"
INACTIVE = " "
UNKNOWN = "–"
WARNING = "⚠"
RESET = "↻"
SEPARATOR = "  "
PALETTE = {"ok": "38;5;114", "warn": "38;5;179", "hot": "38;5;203"}
DIM = "2"
BOLD = "1"
ACTIVE_COLOR = "38;5;111"
HOT_BOLD_AT = 95
BAR_WIDTH = 8
RESET_WIDTH = 6
WARNING_WIDTH = 2 + max(len(w) for w in ("expired", "missing", "no-plan")) + 1
WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}
AUTH_WARNINGS = frozenset({"expired", "missing", "no-plan"})


def color_enabled(stream: TextIO | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return stream is None or stream.isatty()


def paint(text: str, *sgr: str, on: bool) -> str:
    return f"\x1b[{';'.join(sgr)}m{text}\x1b[0m" if on else text


def bar(used: float, width: int = BAR_WIDTH) -> str:
    full = round(max(0, min(100, used)) * width / 100)
    return FULL * full + EMPTY * (width - full)


def level(used: float) -> Literal["ok", "warn", "hot"]:
    if used < 50:
        return "ok"
    return "warn" if used < 80 else "hot"


def until(resets_at: int | None, now: int) -> str:
    if resets_at is None or resets_at <= now:
        return ""
    m = -(-(resets_at - now) // 60)
    if m < 60:
        return f"{RESET}{m}m"
    h, m = divmod(m, 60)
    if h < 10:
        return f"{RESET}{h}h{m:02d}m"
    if h < 24:
        return f"{RESET}{h}h"
    d, h = divmod(h, 24)
    return f"{RESET}{d}d{h}h"


def pool_lines(cfg: Config, st: dict, now: int, *, name_width: int, color: bool) -> list[str]:
    outlook = chain.summary(cfg, st, now)
    if outlook is None or outlook.total == 0:
        return []
    used5 = max(0, min(100, 100 * (1 - outlook.room / outlook.total)))
    title = paint(f"{'pool':<{name_width}}", DIM, on=color)
    prefix = f"  {title}{SEPARATOR}{' ' * 8}{' ' * 4}{SEPARATOR}"
    cells = [usage_cell("5h", used5, None, now, color=color),
             usage_cell("7d", outlook.week_used, None, now, color=color)]
    parts = []
    if outlook.runway_s is not None:
        duration = until(now + math.ceil(outlook.runway_s), now).removeprefix(RESET)
        if "h" in duration and duration.endswith("m"):
            duration = duration[:-1]
        parts.append(f"lasts ~{duration or '0m'}")
    if outlook.next_at is not None:
        clock = time.strftime("%H:%M", time.localtime(outlook.next_at))
        parts.append(f"next reset {outlook.next_account} {clock}")
        if outlook.runway_s is not None:
            if outlook.seamless:
                parts.append("no gap")
            elif outlook.gap_at is not None:
                clock = time.strftime("%H:%M", time.localtime(outlook.gap_at))
                parts.append(f"gap ~{round(outlook.gap_s / 60)}m at {clock}")
    indent = " " * (2 + name_width + len(SEPARATOR) + 12 + len(SEPARATOR))
    rows = [(prefix + SEPARATOR.join(cells)).rstrip()]
    if parts:
        rows.append(indent + paint(" · ".join(parts), DIM, on=color))
    return rows


def window_cell(acct: dict, key: str, now: int, *, color: bool) -> str:
    used, resets_at = router.win(acct, key, now) if acct else (None, None)
    return usage_cell(WINDOW_LABELS[key], used, resets_at, now, color=color)


def usage_cell(label: str, used: float | None, resets_at: int | None, now: int, *, color: bool) -> str:
    if used is None:
        return f"{label} {paint(EMPTY * BAR_WIDTH, DIM, on=color)} {UNKNOWN:>4} {'':<{RESET_WIDTH}}"
    used = max(0.0, min(100.0, used))
    sgr = (PALETTE[level(used)], BOLD) if used >= HOT_BOLD_AT else (PALETTE[level(used)],)
    usage = paint(f"{bar(used)} {used:>3.0f}%", *sgr, on=color)
    reset = until(resets_at, now)
    return f"{label} {usage} " + paint(reset, DIM, on=color and bool(reset)) + " " * (RESET_WIDTH - len(reset))


def window_label(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def codex_line(snap: Snapshot, now: int, *, name_width: int, color: bool) -> str:
    cells = []
    for minutes, used, resets_at in snap["windows"]:
        if resets_at is None or resets_at <= now:
            used, resets_at = 0.0, None
        cells.append(usage_cell(window_label(minutes), used, resets_at, now, color=color))
    if not cells:
        cells = [usage_cell(label, None, None, now, color=color) for label in ("5h", "7d")]
    marker = paint(INACTIVE, DIM, on=color)
    row = f"{marker} {'codex':<{name_width}}{SEPARATOR}{'':<12}{SEPARATOR}" + SEPARATOR.join(cells)
    suffix = f" {snap['plan'] or '?'}"
    ts = snap["ts"]
    if ts is not None and now - ts > 3600:
        age = int(now - ts)
        value, unit = (age // 86400, "d") if age >= 86400 else (age // 3600, "h")
        suffix += f" · {value}{unit} ago"
    return row + paint(suffix, DIM, on=color)


def shard_header(cfg: Config, st: dict, now: int) -> tuple[str | None, set[str]]:
    count = len({s.get("pid") for s in (st.get("sessions") or {}).values()
                 if s.get("shard") is True and state.pid_alive(s.get("pid"))})
    if os.environ.get("CPOOL_SHARD") != "1" and not count:
        return None, set()
    _, serving = shardacct.serving(cfg, st, now)
    _, unmatched = shardacct.live_by_account(cfg, st, now)
    labels = " ".join(f"{name}×{number}" for name, number in sorted(unmatched.items(), key=lambda item: (-item[1], item[0])))
    return f"⇄ shard {count}" + (f" · {labels}" if labels else ""), serving


def account_line(name: str, acct: dict, now: int, *, active: bool, name_width: int, color: bool,
                 serving: set[str] | None = None, live: int = 0, stale_after_s: int = 180) -> str:
    if os.environ.get("CPOOL_SHARD") == "1":
        active = serving is not None and name in serving
    auth = acct.get("auth")
    muted = auth in AUTH_WARNINGS
    marker = paint(ACTIVE if active else INACTIVE, ACTIVE_COLOR if active else DIM, on=color and not muted)
    title = paint(f"{name:<{name_width}}", BOLD, ACTIVE_COLOR, on=color and active and not muted)
    cells = [window_cell(acct, key, now, color=color and not muted) for key in WINDOW_LABELS]
    glyphs = "●" * min(live, 4) if live else "·"
    if live > 4:
        glyphs += f"+{live - 4}"
    sessions = paint(f"{glyphs:<8}", ACTIVE_COLOR if active else DIM, on=color) + f" {live:>3}"
    row = f"{marker} {title}{SEPARATOR}{sessions}{SEPARATOR}" + SEPARATOR.join(cells)
    ts = acct.get("updated_at")
    if stale_after_s and ts is not None and now - ts > stale_after_s:
        age = int(now - ts)
        value, unit = ((age // 86400, "d") if age >= 86400 else
                       (age // 3600, "h") if age >= 3600 else (age // 60, "m"))
        row = row.rstrip() + paint(f" · {value}{unit} ago", DIM, on=color)
    if muted:
        return paint(row, DIM, on=color) + paint(f" {WARNING} {auth}", PALETTE["warn"], on=color)
    return row.rstrip()


def account_lines(cfg: Config, st: dict, active: str | None, now: int, *, color: bool,
                  min_width: int = 0, serving: set[str] | None = None) -> list[str]:
    live, _ = shardacct.live_by_account(cfg, st, now)
    enabled = [a for a in cfg.accounts if a.enabled or live[a.name]]
    width = max(min_width, max((len(a.name) for a in enabled), default=0))
    stored = st.get("accounts") or {}
    header, selected = shard_header(cfg, st, now)
    serving = selected if header is not None and serving is None else serving
    lines = [header] if header is not None else []
    lines.extend(account_line(a.name, stored.get(a.name) or {}, now, active=a.name == active,
                              name_width=width, color=color, serving=serving, live=live[a.name],
                              stale_after_s=3 * cfg.pool.live_poll_s) for a in enabled)
    return lines
