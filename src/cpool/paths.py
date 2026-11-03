"""Filesystem layout under CPOOL_HOME (default ~/.cpool)."""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    env = os.environ.get("CPOOL_HOME")
    return Path(env).expanduser() if env else Path.home() / ".cpool"


def config_path() -> Path:
    return home() / "config.toml"


def state_path() -> Path:
    return home() / "state.json"


def handoff_dir() -> Path:
    return home() / "handoff"


def log_dir() -> Path:
    return home() / "log"


def shared_dir() -> Path:
    return home() / "shared"


def accounts_dir() -> Path:
    return home() / "accounts"


def advisor_dir() -> Path:
    return home() / "advisor"
