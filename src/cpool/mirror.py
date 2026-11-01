"""Share transcripts/settings across account dirs via symlinks into ~/.cpool/shared.

Never deletes a file: collisions are renamed (`.cpool-conflict-<acct>` inside shared,
`.cpool-bak-<acct>` inside the account dir) and every step is journalled.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import journal, paths

SHARED: tuple[str, ...] = (
    "projects", "settings.json", "CLAUDE.md", "skills", "agents",
    "plugins", "commands", "hooks", "history.jsonl", "file-history", "todos", "plans",
)
PER_ACCOUNT: tuple[str, ...] = (
    ".claude.json", "daemon", "ide", ".credentials.json", "statsig", "shell-snapshots",
)
_FILES = frozenset({"settings.json", "CLAUDE.md", "history.jsonl"})


def _same(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _free(path: Path) -> Path:
    if not os.path.lexists(path):
        return path
    i = 2
    while os.path.lexists(path.with_name(f"{path.name}.{i}")):
        i += 1
    return path.with_name(f"{path.name}.{i}")


def _step(actions: list[str], acct: str, msg: str) -> None:
    actions.append(msg)
    journal.log("mirror", account=acct, action=msg)


def _move(src: Path, dst: Path, actions: list[str], acct: str) -> None:
    shutil.move(str(src), str(dst))
    _step(actions, acct, f"moved {src} -> {dst}")


def _merge_dir(entry: str, src: Path, dst: Path, acct: str, actions: list[str]) -> None:
    for child in sorted(src.iterdir()):
        target = dst / child.name
        if not os.path.lexists(target):
            _move(child, target, actions, acct)
        elif (
            entry == "projects"
            and child.is_dir() and not child.is_symlink()
            and target.is_dir() and not target.is_symlink()
        ):
            for f in sorted(child.iterdir()):
                t2 = target / f.name
                if not os.path.lexists(t2):
                    _move(f, t2, actions, acct)
                else:
                    _move(f, _free(target / f"{f.name}.cpool-conflict-{acct}"), actions, acct)
            try:
                child.rmdir()
            except OSError:
                _step(actions, acct, f"left non-empty {child}")
        else:
            _move(child, _free(dst / f"{child.name}.cpool-conflict-{acct}"), actions, acct)


def _merge_file(entry: str, src: Path, dst: Path, acct: str, actions: list[str]) -> None:
    bak = src.with_name(f"{entry}.cpool-bak-{acct}")
    if entry != "history.jsonl":
        bak = _free(bak)
        os.replace(src, bak)
        _step(actions, acct, f"backed up {src} -> {bak}")
        return
    data = src.read_bytes()
    full = data if data.endswith(b"\n") or not data else data + b"\n"
    # Re-run after a crash: the backup matches and shared already ends with it.
    done = (
        os.path.lexists(bak) and bak.read_bytes() == data
        and (not full or dst.read_bytes().endswith(full))
    )
    if not done:
        bak = _free(bak)
        shutil.copy2(src, bak)
        _step(actions, acct, f"backed up {src} -> {bak}")
        if full:
            # Atomic: a crash leaves either the old shared file or the complete new one.
            real = Path(os.path.realpath(dst))
            old = real.read_bytes()
            if old and not old.endswith(b"\n"):
                old += b"\n"
            tmp = real.with_name(f"{real.name}.cpool-tmp")
            with open(tmp, "wb") as f:
                f.write(old + full)
                f.flush()
                os.fsync(f.fileno())
            shutil.copymode(real, tmp)
            os.replace(tmp, real)
            _step(actions, acct, f"appended {src} -> {dst}")
    os.unlink(src)
    _step(actions, acct, f"removed {src} (kept as {bak})")


def link(acct_dir: Path) -> list[str]:
    acct_dir = Path(acct_dir)
    acct = acct_dir.name.lstrip(".") or "account"
    shared = paths.shared_dir()
    actions: list[str] = []
    for entry in SHARED:
        p, sh = acct_dir / entry, shared / entry
        if p.is_symlink():
            if _same(p, sh):
                continue
            _step(actions, acct, f"skipped {p}: symlink points elsewhere ({os.readlink(p)})")
            continue
        is_file = entry in _FILES
        if not os.path.lexists(p):
            if not os.path.lexists(sh):
                if is_file:
                    continue
                sh.mkdir(parents=True, exist_ok=True)
        elif not os.path.lexists(sh):
            shared.mkdir(parents=True, exist_ok=True)
            _move(p, sh, actions, acct)
        elif p.is_dir() and sh.is_dir() and not sh.is_symlink():
            _merge_dir(entry, p, sh, acct, actions)
            try:
                p.rmdir()
            except OSError:
                _step(actions, acct, f"left non-empty {p}; not linked")
                continue
        elif p.is_file() and sh.is_file():
            _merge_file(entry, p, sh, acct, actions)
        else:
            _move(p, _free(shared / f"{entry}.cpool-conflict-{acct}"), actions, acct)
        acct_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(sh, p)
        _step(actions, acct, f"linked {p} -> {sh}")
    return actions


def check(acct_dir: Path) -> list[str]:
    acct_dir = Path(acct_dir)
    shared = paths.shared_dir()
    problems: list[str] = []
    for entry in SHARED:
        p, sh = acct_dir / entry, shared / entry
        if p.is_symlink():
            if not _same(p, sh):
                problems.append(f"{p} points to {os.readlink(p)}, not {sh}")
        elif os.path.lexists(p):
            problems.append(f"{p} is a real {'dir' if p.is_dir() else 'file'}, not linked to shared")
        elif entry not in _FILES:
            problems.append(f"{p} is missing")
    for entry in PER_ACCOUNT:
        p = acct_dir / entry
        if p.is_symlink():
            problems.append(f"{p} is a symlink but must be per-account")
    return problems
