"""`cpool run`: route -> spawn claude -> on exit take a handoff -> jitter -> relaunch --resume."""
from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import accounts, config, handoff, journal, paths, router, state

MAX_RELAUNCHES = 5
_warned_missing: set[str] = set()  # --settings paths already reported this `cpool run`

# Flags taking exactly one value, from `claude --help` (claude 2.x, 2026-11-01).
VALUE_FLAGS = frozenset({
    "--agent", "--agents", "--append-system-prompt", "--autocompact", "--debug-file",
    "--effort", "--environment", "--fallback-model", "--input-format", "--json-schema",
    "--max-budget-usd", "--model", "-n", "--name", "--output-format", "--permission-mode",
    "--permission-prompts", "--plugin-dir", "--plugin-url", "--remote-control-session-name-prefix",
    "--setting-sources", "--settings", "--system-prompt", "--system-prompt-snapshot",
})
# Variadic flags (`<x...>`): consume following non-dash args.
VARIADIC_FLAGS = frozenset({
    "--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
    "--betas", "--file", "--mcp-config", "--tools",
})
# Optional-value flags (`[x]`): drop with a following non-dash arg (they replace --resume),
# or keep the flag and its optional value.
_DROP_VALUE = ("--resume", "-r", "--session-id", "--from-pr", "--teleport")
_OPTIONAL_VALUE_FLAGS = frozenset({
    "-d", "--debug", "--remote-control", "--cloud", "-w", "--worktree", "--prompt-suggestions",
})
_DROP_FLAG = frozenset({"--continue", "-c", "--fork-session"})


def relaunch_args(args: list[str]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            break  # everything after is the prompt
        if a in _DROP_VALUE:
            i += 1
            if i < n and not args[i].startswith("-"):
                i += 1
        elif any(a.startswith(f + "=") for f in _DROP_VALUE) or a in _DROP_FLAG:
            i += 1
        elif a in VALUE_FLAGS:
            out.extend(args[i:i + 2])
            i += 2
        elif a in _OPTIONAL_VALUE_FLAGS:
            out.append(a)
            i += 1
            if i < n and not args[i].startswith("-"):
                out.append(args[i])
                i += 1
        elif a in VARIADIC_FLAGS:
            out.append(a)
            i += 1
            while i < n and not args[i].startswith("-"):
                out.append(args[i])
                i += 1
        elif a.startswith("-"):
            out.append(a)
            i += 1
        else:
            i += 1  # positional prompt
    return out


def _is_print(args: list[str]) -> bool:
    for a in args:
        if a == "--":
            return False
        if a in ("-p", "--print"):
            return True
    return False


def _pop_flag(args: list[str], name: str) -> tuple[str | None, list[str]]:
    """Remove `name <v>` / `name=<v>` (last wins); returns (value, rest)."""
    val: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == name and i + 1 < len(args):
            val = args[i + 1]
            i += 2
        elif a.startswith(name + "="):
            val = a[len(name) + 1:]
            i += 1
        else:
            rest.append(a)
            i += 1
    return val, rest


def _parse(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    model = account = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest.extend(argv[i + 1:])
            break
        if a in ("--model", "--account") and i + 1 < len(argv):
            if a == "--model":
                model = argv[i + 1]
            else:
                account = argv[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    return model, account, rest


def _plugin_dir() -> str:
    env = os.environ.get("CPOOL_PLUGIN_DIR")
    if env:
        return env
    import cpool
    pkg = Path(cpool.__file__).parent / "plugin"
    return str(pkg if pkg.exists() else Path(__file__).parents[2] / "plugin")


def _cpool_bin() -> str:
    return shutil.which("cpool") or f"{sys.executable} -m cpool"


def _is_ours(cmd: str) -> bool:
    return "cpool" in cmd and cmd.rstrip().endswith("statusline")


def _load_settings(v: str) -> dict:
    try:
        text = v if v.lstrip().startswith("{") else Path(v).expanduser().read_text()
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError) as e:
        journal.log("error", where="run.settings", error=str(e))
        if isinstance(e, FileNotFoundError) and v not in _warned_missing:
            _warned_missing.add(v)
            _stderr(f"cpool: --settings {v} not found; using cpool settings only")
        return {}


def _statusline_cmd(settings: dict) -> str | None:
    sl = settings.get("statusLine")
    cmd = sl.get("command") if isinstance(sl, dict) else None
    return cmd if isinstance(cmd, str) and cmd and not _is_ours(cmd) else None


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _hhmm(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "unknown"


def _skip_text(reason: str) -> str:
    if reason.startswith("cooldown:"):
        return f"cooldown until {_hhmm(int(reason.split(':', 1)[1]))}"
    return {"limit": "at limit"}.get(reason, reason)


def _route(cfg, *, pinned: str | None, model: str | None, now: int, **kw) -> tuple[str, str, dict] | None:
    """(account, reason, scores) for the first launch, or None (message already printed)."""
    enabled = {a.name: a for a in cfg.accounts if a.enabled}
    if pinned is not None:
        if pinned not in enabled:
            _stderr(f"cpool: no enabled account {pinned!r}")
            return None
        return pinned, "pinned", {}
    st = state.read()
    d = router.choose(st, cfg, model=model or "", cwd=os.getcwd(), now=now, **kw)
    if d.account is not None:
        return d.account, d.reason, d.scores
    accts = st.get("accounts") or {}
    cands = [n for n in enabled if (accts.get(n) or {}).get("credits_enabled") is False]
    if not cands:
        _stderr(f"cpool: no account available ({d.reason}); earliest reset {_hhmm(d.earliest_reset)}")
        return None
    best = min(cands, key=lambda n: router.win(accts.get(n) or {}, "five_hour", now)[0])
    _stderr(f"cpool: no account under thresholds; using {best} (earliest reset {_hhmm(d.earliest_reset)})")
    return best, "fallback", {}


def _child_signals() -> None:
    # SIG_IGN survives exec; the child must see the default dispositions.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)


def _next_statusline(user_settings: str | None, acct) -> str | None:
    """The user's own statusLine to chain: --settings first, else the account's settings.json."""
    if user_settings is not None:
        cmd = _statusline_cmd(_load_settings(user_settings))
        if cmd:
            return cmd
    sj = accounts.storage_dir(acct) / "settings.json"
    return _statusline_cmd(_load_settings(str(sj))) if sj.exists() else None


def _spawn(cmd: list[str], env: dict[str, str]) -> int:
    old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
    old_quit = signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    try:
        return subprocess.Popen(cmd, env=env, preexec_fn=_child_signals).wait()
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGQUIT, old_quit)


def _hard_handoff(run_id: str, model: str | None, now: int) -> dict | None:
    """No handoff file: if this run's account hit a hard limit, offer to move on."""
    st = state.read()
    mine = [(s.get("updated_at") or 0, sid, s) for sid, s in (st.get("sessions") or {}).items()
            if s.get("run") == run_id]
    if not mine:
        return None
    _, sid, sess = max(mine, key=lambda t: t[0])
    name = sess.get("account")
    acct = (st.get("accounts") or {}).get(name) or {}
    fam = router.family(sess.get("model") or model)
    at_limit = router.win(acct, "five_hour", now)[0] >= 99 or (
        fam is not None and router.win(acct.get("model_week") or {}, fam, now)[0] >= 99)
    if not at_limit or not sys.stdin.isatty():
        return None
    try:
        ans = input(f"cpool: {name} is at its limit. Resume on another account? [Y/n] ")
    except EOFError:
        return None
    if ans.strip().lower() not in ("", "y", "yes"):
        return None
    return {"session_id": sid, "from": name, "model": sess.get("model") or model or "",
            "reason": "hard", "compacted": False}


def main(argv: list[str]) -> int:
    cpool_model, pinned, args = _parse(argv)
    _warned_missing.clear()
    cfg = config.load()
    if not any(a.enabled for a in cfg.accounts):
        _stderr("cpool: no enabled accounts; running claude directly")
        os.execvp("claude", ["claude", *args])

    user_model, args = _pop_flag(args, "--model")
    model = user_model or cpool_model
    user_settings, args = _pop_flag(args, "--settings")
    print_mode = _is_print(args)

    now = int(time.time())
    routed = _route(cfg, pinned=pinned, model=model, now=now)
    if routed is None:
        return 1
    acct_name, reason, scores = routed
    by_name = {a.name: a for a in cfg.accounts}

    run_id = uuid.uuid4().hex
    plugin = _plugin_dir()
    ours = {"statusLine": {"type": "command", "command": f"{_cpool_bin()} statusline"},
            "autoContinueAtUsageLimit": False}
    if user_settings is not None:
        base = _load_settings(user_settings)
    else:
        sj = accounts.storage_dir(by_name[acct_name]) / "settings.json"
        base = _load_settings(str(sj)) if sj.exists() else {}
    settings_file = paths.home() / "run" / f"{run_id}.settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({**base, **ours}))

    try:
        journal.log("route", run=run_id, account=acct_name, reason=reason, scores=scores)
        relaunches = 0
        while True:
            acct = by_name[acct_name]
            env = dict(os.environ)
            for k, v in accounts.launch_env(acct).items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = v
            env["CPOOL_RUN"] = run_id
            env["CPOOL_ACCOUNT"] = acct_name
            next_cmd = _next_statusline(user_settings, acct)
            if next_cmd:
                env["CPOOL_STATUSLINE_NEXT"] = next_cmd
            else:
                env.pop("CPOOL_STATUSLINE_NEXT", None)
            cmd = ["claude", *args]
            if model:
                cmd += ["--model", model]
            cmd += ["--plugin-dir", plugin, "--settings", str(settings_file)]
            code = _spawn(cmd, env)

            now = int(time.time())
            h = handoff.take(run_id) or (None if print_mode else _hard_handoff(run_id, model, now))
            if h is None:
                return code
            src = h["from"]
            with state.locked() as st:
                if src in by_name:
                    state.account_state(st, src)["left_at"] = now
                sess = (st.get("sessions") or {}).get(h["session_id"])
                if isinstance(sess, dict):
                    sess["leave"] = None
                    sess["leave_manual"] = False
            if print_mode:
                _stderr(f"cpool: {src} over threshold; next run will route elsewhere")
                return code
            if relaunches >= MAX_RELAUNCHES:
                _stderr(f"cpool: {MAX_RELAUNCHES} relaunches reached; stopping "
                        f"(resume manually: claude --resume {h['session_id']})")
                return code
            st = state.read()
            st = {**st, "sessions": {k: v for k, v in (st.get("sessions") or {}).items()
                                     if k != h["session_id"]}}
            model = h.get("model") or model
            d = router.choose(st, cfg, model=model or "", cwd=os.getcwd(), now=now,
                              current=src, exclude=frozenset({src, acct_name}), forced=True,
                              ignore_cooldown=h.get("reason") == "manual")
            if d.account is None:
                why = ", ".join(f"{n}: {_skip_text(r)}" for n, r in d.skipped.items())
                reset = (f"; earliest reset {_hhmm(d.earliest_reset)}"
                         if any(r == "limit" for r in d.skipped.values()) else "")
                _stderr(f"cpool: no account to move to ({why}){reset}; "
                        f"resume manually: claude --resume {h['session_id']}")
                return code
            wait = random.uniform(*cfg.pool.jitter_s)
            _stderr(f"cpool: moving session to {d.account} in {wait:.0f}s ({h.get('reason')})")
            time.sleep(wait)
            journal.log("switch", run=run_id, session=h["session_id"], **{"from": src},
                        to=d.account, reason=h.get("reason"), compacted=h.get("compacted"))
            acct_name = d.account
            args = [*relaunch_args(args), "--resume", h["session_id"]]
            relaunches += 1
    finally:
        try:
            settings_file.unlink()
        except OSError:
            pass
