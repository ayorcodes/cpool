"""Shared account usage display, independent of terminal and state I/O."""
from __future__ import annotations

import os
from typing import Literal, TextIO

from . import router
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
    row = f"{marker} {'codex':<{name_width}}{SEPARATOR}" + SEPARATOR.join(cells)
    suffix = f" {snap['plan'] or '?'}"
    ts = snap["ts"]
    if ts is not None and now - ts > 3600:
        age = int(now - ts)
        value, unit = (age // 86400, "d") if age >= 86400 else (age // 3600, "h")
        suffix += f" · {value}{unit} ago"
    return row + paint(suffix, DIM, on=color)


def account_line(name: str, acct: dict, now: int, *, active: bool, name_width: int, color: bool) -> str:
    auth = acct.get("auth")
    muted = auth in AUTH_WARNINGS
    marker = paint(ACTIVE if active else INACTIVE, ACTIVE_COLOR if active else DIM, on=color and not muted)
    title = paint(f"{name:<{name_width}}", BOLD, ACTIVE_COLOR, on=color and active and not muted)
    cells = [window_cell(acct, key, now, color=color and not muted) for key in WINDOW_LABELS]
    row = f"{marker} {title}{SEPARATOR}" + SEPARATOR.join(cells)
    if muted:
        return paint(row, DIM, on=color) + paint(f" {WARNING} {auth}", PALETTE["warn"], on=color)
    return row.rstrip()


def account_lines(cfg: Config, st: dict, active: str | None, now: int, *, color: bool,
                  min_width: int = 0) -> list[str]:
    enabled = [a for a in cfg.accounts if a.enabled]
    width = max(min_width, max((len(a.name) for a in enabled), default=0))
    stored = st.get("accounts") or {}
    return [account_line(a.name, stored.get(a.name) or {}, now, active=a.name == active,
                         name_width=width, color=color) for a in enabled]
