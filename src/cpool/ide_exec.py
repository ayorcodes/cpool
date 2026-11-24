"""`cpool-ide-exec`: VS Code `claudeCode.claudeProcessWrapper` target. Invoked as
`cpool-ide-exec <claude-bin> [args...]`; picks an account, then execs claude in place
(no proxy, no child process, stdio untouched)."""
from __future__ import annotations

import os
import sys
import time

from . import accounts, config, journal, run, state, usage

POLL_WAIT_S = 1.5
PASSTHROUGH_FLAGS = frozenset({"-v", "--version", "-h", "--help"})
PASSTHROUGH_SUBCOMMANDS = frozenset({
    "auth", "config", "doctor", "install", "mcp", "migrate-installer", "plugin",
    "setup-token", "update", "upgrade",
})
_OPTIONAL_VALUE = run._OPTIONAL_VALUE_FLAGS | frozenset(run._DROP_VALUE)
_SESSION_ENV = ("CPOOL_RUN", "CPOOL_RESUMED", "CPOOL_SHARD", "CPOOL_STATUSLINE_NEXT")


def _flag_end(args: list[str], i: int) -> int:
    """Index after the flag at `i` and the value(s) it consumes."""
    a = args[i]
    if a in run.VALUE_FLAGS:
        return i + 2
    j = i + 1
    if a in run.VARIADIC_FLAGS:
        while j < len(args) and not args[j].startswith("-"):
            j += 1
    elif a in _OPTIONAL_VALUE and j < len(args) and not args[j].startswith("-"):
        j += 1
    return j


def is_passthrough(args: list[str]) -> bool:
    """True for helper invocations (`--version`, `plugin list`, ...) that need no account."""
    seen_print = False
    first_pos: str | None = None
    i = 0
    while i < len(args) and args[i] != "--":
        a = args[i]
        if a in PASSTHROUGH_FLAGS:
            return True
        if a in ("-p", "--print"):
            seen_print = True
        if a.startswith("-"):
            i = _flag_end(args, i)
        else:
            if first_pos is None:
                first_pos = a
            i += 1
    return not seen_print and first_pos in PASSTHROUGH_SUBCOMMANDS


def extract_model(args: list[str]) -> str | None:
    model: str | None = None
    i = 0
    while i < len(args) and args[i] != "--":
        a = args[i]
        if a == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        elif a.startswith("--model="):
            model = a[len("--model="):]
            i += 1
        elif a.startswith("-"):
            i = _flag_end(args, i)
        else:
            i += 1
    return model


def _exec(claude_bin: str, argv: list[str], env: dict[str, str]) -> int:
    try:
        os.execvpe(claude_bin, argv, env)
    except OSError as e:
        print(f"cpool: failed to exec {claude_bin}: {e}", file=sys.stderr)
    return 127


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: cpool-ide-exec <claude-bin> [args...]", file=sys.stderr)
        return 2
    claude_bin, claude_args = argv[0], argv[1:]
    if is_passthrough(claude_args):
        return _exec(claude_bin, argv, dict(os.environ))

    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"cpool: {e}", file=sys.stderr)
        return 2
    if not any(a.enabled for a in cfg.accounts):
        print("cpool: no enabled accounts; running claude directly", file=sys.stderr)
        return _exec(claude_bin, argv, dict(os.environ))

    now = int(time.time())
    if usage.poll_due(cfg, state.read(), now):
        usage.kick(cfg, now, wait_s=POLL_WAIT_S)

    routed = run.route(cfg, model=extract_model(claude_args), now=int(time.time()))
    if routed is None:
        return 1
    acct_name, reason, scores = routed
    journal.log("route", account=acct_name, reason=reason, scores=scores, ide=True)

    env = dict(os.environ)
    for k, v in accounts.launch_env(next(a for a in cfg.accounts if a.name == acct_name)).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    for k in _SESSION_ENV:
        env.pop(k, None)
    env["CPOOL_ACCOUNT"] = acct_name
    env["CPOOL_IDE"] = "1"
    return _exec(claude_bin, argv, env)
