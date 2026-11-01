from __future__ import annotations

import os
import plistlib
import subprocess
import time
from pathlib import Path

from . import paths, schedule
from .config import Config

PREFIX = "com.cpool."
WARM = PREFIX + "warm"
POLL = PREFIX + "poll"


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plists(cfg: Config, cpool_bin: str) -> dict[str, dict]:
    log = paths.log_dir()
    return {
        WARM: {
            "Label": WARM,
            "ProgramArguments": [cpool_bin, "warm", "--due"],
            "StartCalendarInterval": [
                {"Hour": s.hour, "Minute": s.minute} for s in schedule.plan(cfg)
            ],
            "StandardOutPath": str(log / "warm.out"),
            "StandardErrorPath": str(log / "warm.err"),
        },
        POLL: {
            "Label": POLL,
            "ProgramArguments": [cpool_bin, "poll"],
            "StartInterval": cfg.pool.poll_min * 60,
            "RunAtLoad": True,
            "StandardOutPath": str(log / "poll.out"),
            "StandardErrorPath": str(log / "poll.err"),
        },
    }


def _launchctl(*args: str) -> int:
    return subprocess.run(
        ["launchctl", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def _bootstrap(domain: str, path: Path) -> None:
    for attempt in (0, 1):
        r = subprocess.run(
            ["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True
        )
        if r.returncode == 0:
            return
        if attempt == 0:
            time.sleep(1)
    raise RuntimeError(f"launchctl bootstrap {path.name} failed: {r.stderr.strip()}")


def install(cfg: Config, cpool_bin: str, dry_run: bool = False) -> dict[str, dict]:
    plists = _plists(cfg, cpool_bin)
    if dry_run:
        return plists
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    _agents_dir().mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for label, data in plists.items():
        path = _agents_dir() / f"{label}.plist"
        path.write_bytes(plistlib.dumps(data))
        _launchctl("bootout", f"{domain}/{label}")  # not loaded yet is fine
        _bootstrap(domain, path)
    return plists


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    for label in (WARM, POLL):
        _launchctl("bootout", f"{domain}/{label}")
        (_agents_dir() / f"{label}.plist").unlink(missing_ok=True)


def pmset_apply(cfg: Config) -> str | None:
    """The wake command for the CLI to print; never runs sudo."""
    slots = schedule.plan(cfg)
    if not slots:
        return None
    m = (min(s.hour * 60 + s.minute for s in slots) - 3) % 1440
    return f"sudo pmset repeat wakeorpoweron MTWRFSU {m // 60:02d}:{m % 60:02d}:00"
