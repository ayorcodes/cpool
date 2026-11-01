"""Account registry operations. Never reads, writes or deletes a Keychain item."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from . import config, journal, mirror, paths
from .config import Account, Config


class AccountError(RuntimeError):
    pass


def keychain_service(config_dir: str | None) -> str:
    if config_dir is None:
        return "Claude Code-credentials"
    return "Claude Code-credentials-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8]


def launch_env(acct: Account) -> dict[str, str | None]:
    return {"CLAUDE_CONFIG_DIR": None if acct.dir is None else str(acct.dir)}


def storage_dir(acct: Account) -> Path:
    return Path(os.path.realpath(acct.dir or Path.home() / ".claude"))


def resolve_account(cfg: Config, config_dir: str | None) -> Account | None:
    if config_dir is None:
        return next((a for a in cfg.accounts if a.dir is None), None)
    real = os.path.realpath(os.path.expanduser(config_dir))
    return next((a for a in cfg.accounts if str(storage_dir(a)) == real), None)


def _check_name(name: str, cfg: Config) -> None:
    if not config._NAME_RE.fullmatch(name):
        raise AccountError(f"name {name!r} must match [a-z0-9-]+")
    if any(a.name == name for a in cfg.accounts):
        raise AccountError(f"account {name!r} already exists")


def _next_order(cfg: Config) -> int:
    return max((a.order for a in cfg.accounts), default=0) + 1


def _claude_running() -> bool:
    return subprocess.run(["pgrep", "-x", "claude"], capture_output=True).returncode == 0


def _guard(force: bool) -> None:
    """Every path that runs mirror.link goes through here."""
    if not force and _claude_running():
        raise AccountError("a claude process is running; quit it first (or pass force)")


def _register(cfg: Config, name: str, launch: Path | None, force: bool = False) -> Account:
    acct = Account(name, launch, _next_order(cfg))
    _guard(force)
    mirror.link(storage_dir(acct))
    config.save_account(acct)  # last: a crash before this leaves the adopt re-runnable
    return acct


def add(name: str, force: bool = False) -> Account:
    cfg = config.load()
    _check_name(name, cfg)
    _guard(force)
    target = paths.accounts_dir() / name
    if target.exists():
        raise AccountError(f"{target} already exists")
    target.mkdir(parents=True)
    acct = _register(cfg, name, target, force)
    journal.log("adopt", account=name, mode="add", dir=str(target))
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env["CLAUDE_CONFIG_DIR"] = str(target)
    # Interactive; output is never captured.
    subprocess.run(["claude", "auth", "login"], env=env, check=False)
    return acct


def adopt(src: Path, name: str, move: bool, force: bool = False) -> Account:
    cfg = config.load()
    src = Path(os.path.expanduser(str(src)))
    if not src.is_absolute():
        raise AccountError(f"{src} must be an absolute path")
    target = paths.accounts_dir() / name
    # A crash between the move and the symlink leaves src gone and target a real dir.
    resume = move and not os.path.lexists(src) and target.is_dir() and not target.is_symlink()
    if not resume and not os.path.lexists(src):
        raise AccountError(f"{src} does not exist")
    existing = next((a for a in cfg.accounts if a.name == name), None)
    if existing is not None and str(storage_dir(existing)) == os.path.realpath(src):
        _guard(force)
        mirror.link(storage_dir(existing))  # idempotent finish
        return existing
    _check_name(name, cfg)
    _guard(force)
    launch: Path | None = None if str(src) == str(Path.home() / ".claude") else src
    if move:
        accounts = os.path.realpath(paths.accounts_dir())
        if resume:
            os.symlink(target, src)
            journal.log("adopt", account=name, mode="move", src=str(src), dir=str(target), resumed=True)
        elif src.is_symlink():
            if not os.path.realpath(src).startswith(accounts + os.sep):
                raise AccountError(f"{src} is a symlink outside {paths.accounts_dir()}; refusing to move")
        else:
            if os.path.lexists(target):
                raise AccountError(f"{target} already exists")
            paths.accounts_dir().mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            os.symlink(target, src)
            journal.log("adopt", account=name, mode="move", src=str(src), dir=str(target))
    acct = _register(cfg, name, launch, force)
    journal.log("adopt", account=name, mode="register", dir=None if launch is None else str(launch))
    return acct


def remove(name: str) -> None:
    config.remove_account(name)
    journal.log("adopt", account=name, mode="remove")
