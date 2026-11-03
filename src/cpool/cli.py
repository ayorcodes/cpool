from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import accounts, advisor, config, journal, launchd, mirror, router, schedule, state, usage, warm


class CliError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cpool",
        description="Pool Claude subscriptions.",
        epilog="cpool [claude flags] runs a session on the best account, e.g. cpool --model opus.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="create an account dir and log in")
    s.add_argument("name")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("adopt", help="register an existing config dir")
    s.add_argument("dir")
    s.add_argument("--name", required=True)
    s.add_argument("--move", action="store_true")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("remove", help="deregister an account")
    s.add_argument("name")

    sub.add_parser("list", help="list accounts")

    s = sub.add_parser("status", help="usage per account")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("switch", help="hand a session off at the end of its next turn")
    s.add_argument("session", nargs="?")

    s = sub.add_parser("plan", help="print the warm-up schedule")
    s.add_argument("--apply", action="store_true")

    sub.add_parser("unplan", help="remove the launchd agents")

    s = sub.add_parser("warm", help="start 5h windows with a Haiku call")
    s.add_argument("--due", action="store_true")
    s.add_argument("account", nargs="?")

    s = sub.add_parser("poll", help="refresh usage from the usage endpoint")
    s.add_argument("--daemon", action="store_true")

    sub.add_parser("statusline", help="statusline wrapper (reads stdin)")
    sub.add_parser("run", help="run claude on the best account: cpool run [claude args]")
    sub.add_parser("doctor", help="check the setup")

    s = sub.add_parser("advise", help="ask the Fable advisor (API credits)")
    adv = s.add_subparsers(dest="advise_cmd", required=True)
    a = adv.add_parser("ask", help="ask a question with a facts file")
    a.add_argument("question", nargs="?")
    a.add_argument("--facts", required=True, type=Path)
    a.add_argument("--attach", action="append", default=[], type=Path)
    a.add_argument("--thread", default="new")
    a.add_argument("--model")
    a.add_argument("--effort", choices=config._EFFORTS)
    a.add_argument("--json", action="store_true")
    a = adv.add_parser("key", help="manage the API key in the Keychain")
    a.add_argument("action", choices=["set", "clear", "status"])
    a.add_argument("--check", action="store_true", help="with status: check the key reaches the model")
    a = adv.add_parser("usage", help="spend this credit cycle")
    a.add_argument("--json", action="store_true")
    a = adv.add_parser("setup", help="store the key, install the fable skill and allow the command")
    a.add_argument("--force", action="store_true")
    a.add_argument("--remove", action="store_true", help="undo the skill and allow rule (the key stays)")
    return p


def _tier_short(tier: str | None) -> str:
    t = tier or ""
    if "max_20x" in t:
        return "Max20x"
    if "max_5x" in t:
        return "Max5x"
    if "claude_ai" in t or "pro" in t:
        return "Pro"
    return "?"


def _window(w: dict | None, now: int) -> dict:
    used, resets_at = router.win({"w": w}, "w", now)
    return {"used": used, "resets_at": resets_at}


def _credits(v) -> str:
    return "?" if v is None else ("ON" if v else "off")


def _age(updated_at, now: int) -> str:
    if not updated_at:
        return "never"
    s = max(0, now - int(updated_at))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _rows(cfg: config.Config, st: dict, now: int) -> list[dict]:
    sessions = st.get("sessions") or {}
    rows = []
    for a in cfg.accounts:
        if not a.enabled:
            continue
        acct = (st.get("accounts") or {}).get(a.name) or {}
        rows.append(
            {
                "name": a.name,
                "tier": _tier_short(acct.get("tier")),
                "five_hour": _window(acct.get("five_hour"), now),
                "seven_day": _window(acct.get("seven_day"), now),
                "model_week": {f: _window(w, now) for f, w in (acct.get("model_week") or {}).items() if w},
                "credits": _credits(acct.get("credits_enabled")),
                "auth": acct.get("auth") or "?",
                "sessions": sum(
                    1 for s in sessions.values() if s.get("account") == a.name and state.pid_alive(s.get("pid"))
                ),
                "source": acct.get("source") or "-",
                "age": _age(acct.get("updated_at"), now),
            }
        )
    return rows


def _pct(w: dict, fmt: str) -> str:
    reset = "-" if w["resets_at"] is None else datetime.fromtimestamp(w["resets_at"]).strftime(fmt)
    return f"{w['used']:.0f}% {reset}"


def _cmd_list() -> int:
    for a in config.load().accounts:
        print(f"{a.name}\t{a.dir or 'default'}\torder={a.order}\t{'enabled' if a.enabled else 'disabled'}")
    return 0


def _cmd_status(as_json: bool) -> int:
    cfg = config.load()
    st = state.read()
    now = int(time.time())
    rows = _rows(cfg, st, now)
    dec = router.choose(st, cfg, model="", cwd=os.getcwd(), now=now)
    if as_json:
        print(json.dumps({"accounts": rows, "next": {"account": dec.account, "reason": dec.reason}}, indent=2))
        return 0
    for r in rows:
        mw = " ".join(f"{f}:{_pct(w, '%a %H:%M')}" for f, w in r["model_week"].items())
        print(
            f"{r['name']:<10} {r['tier']:<7} 5h {_pct(r['five_hour'], '%H:%M'):<10} "
            f"7d {_pct(r['seven_day'], '%a %H:%M'):<14}"
            + (f" {mw}" if mw else "")
            + f" credits={r['credits']} auth={r['auth']} sessions={r['sessions']}"
            f" [{r['source']} {r['age']}]"
        )
    print(f"next: {dec.account or 'none'} ({dec.reason})")
    return 0


def _cmd_switch(session: str | None) -> int:
    cwd = os.getcwd()
    with state.locked() as st:
        sessions = st.get("sessions") or {}
        live = {k: s for k, s in sessions.items() if state.pid_alive(s.get("pid"))}
        if session:
            if session not in sessions:
                raise CliError(f"unknown session: {session}")
            if session not in live:
                raise CliError(f"session {session} is not running")
            sid = session
        else:
            here = [(s.get("updated_at") or 0, k) for k, s in live.items() if s.get("cwd") == cwd]
            if not here:
                raise CliError(f"no live session in {cwd}")
            sid = max(here)[1]
        sessions[sid]["leave_manual"] = True
    if not (state.read().get("sessions") or {}).get(sid, {}).get("leave_manual"):
        raise CliError(f"session {sid} ended before the switch could be recorded")
    journal.log("switch", session=sid, manual=True)
    print(f"session {sid}: handoff happens at the end of its next turn")
    return 0


def _resets(slot: schedule.WarmSlot) -> list[str]:
    start = (slot.hour * 60 + slot.minute) // 30 * 30
    times = [(start + 300 * k) % 1440 for k in range(1, 5)]
    return [f"{t // 60:02d}:{t % 60:02d}" for t in times]


def _cmd_plan(apply: bool) -> int:
    cfg = config.load()
    slots = schedule.plan(cfg)
    if not slots:
        print("no enabled accounts")
    for s in slots:
        print(f"{s.account}: warm {s.hour:02d}:{s.minute:02d}, resets {' '.join(_resets(s))}")
    if apply:
        launchd.install(cfg, _cpool_bin())
        print("launchd agents installed")
        _print_pmset(cfg)
    return 0


def _cpool_bin() -> str:
    return shutil.which("cpool") or os.path.abspath(sys.argv[0])


def _print_pmset(cfg: config.Config) -> None:
    cmd = launchd.pmset_apply(cfg)
    if cmd:
        print(f"run this to wake the Mac before the first warm-up:\n  {cmd}")


def _refresh_schedule() -> None:
    """Keep the launchd warm-up schedule in step with the account list, once it is installed."""
    if not (Path.home() / "Library" / "LaunchAgents" / f"{launchd.WARM}.plist").exists():
        return
    cfg = config.load()
    launchd.install(cfg, _cpool_bin())
    print("schedule updated")
    _print_pmset(cfg)


def _cmd_warm(due: bool, name: str | None) -> int:
    cfg = config.load()
    if due:
        print(f"warmed {warm.warm_due(cfg)}")
        return 0
    targets = [a for a in cfg.accounts if a.enabled and (name is None or a.name == name)]
    if name and not targets:
        raise CliError(f"unknown or disabled account: {name}")
    for a in targets:
        print(f"{a.name}: {'warmed' if warm.warm(a) else 'failed'}")
    return 0


def _cmd_doctor() -> int:
    bad = 0

    def out(ok: bool, msg: str) -> None:
        nonlocal bad
        bad += not ok
        print(("✓ " if ok else "✗ ") + msg)

    try:
        cfg = config.load()
        out(True, f"config loads ({len(cfg.accounts)} accounts)")
    except config.ConfigError as e:
        out(False, f"config: {e}")
        return 1
    out(shutil.which("claude") is not None, "claude on PATH")
    for a in cfg.accounts:
        d = accounts.storage_dir(a)
        out(d.is_dir(), f"{a.name}: storage dir {d}")
        svc = accounts.keychain_service(None if a.dir is None else str(a.dir))
        r = subprocess.run(
            ["security", "find-generic-password", "-s", svc],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        out(r.returncode == 0, f"{a.name}: Keychain item {svc}")
        if d.is_dir():
            for problem in mirror.check(d):
                out(False, f"{a.name}: {problem}")
    try:
        state.read()
        out(True, "state.json parses")
    except Exception as e:  # any parse failure is a finding
        out(False, f"state.json: {e}")
    agents = Path.home() / "Library" / "LaunchAgents"
    for label in (launchd.WARM, launchd.POLL):
        out((agents / f"{label}.plist").exists(), f"launchd {label}")
    return 1 if bad else 0


def _cmd_advise(args) -> int:
    adv = config.load().advisor
    match args.advise_cmd:
        case "ask":
            q = args.question if args.question is not None else sys.stdin.read()
            code, r = advisor.ask(adv, q, args.facts, args.attach, args.thread, args.model, args.effort)
            if r["truncated"]:
                print("cpool: warning: the answer hit max_tokens and is truncated", file=sys.stderr)
            if code == 5:
                print(f"cpool: Fable refused: {json.dumps(r.get('stop_details'))}", file=sys.stderr)
            if args.json:
                print(json.dumps({k: v for k, v in r.items() if k != "stop_details"}))
            else:
                print(r["text"])
                print(f"[advise] thread={r['thread']} round={r['round']}/{r['max_rounds']} model={r['model']} "
                      f"cost=${r['cost_usd']:.4f} cycle=${r['cycle_spent_usd']:.2f}/${r['cap_usd']:.2f}")
            return code
        case "key":
            return _advise_key(args.action, args.check, adv)
        case "usage":
            c = advisor.cycle(adv)
            info = {"cycle_start": datetime.fromtimestamp(c["start"]).isoformat(), "spent_usd": round(c["spent"], 4),
                    "cap_usd": c["cap"], "remaining_usd": round(max(0.0, c["cap"] - c["spent"]), 4),
                    "calls": c["calls"], "open_reservations": c["open"]}
            if args.json:
                print(json.dumps(info))
            else:
                for k, v in info.items():
                    print(f"{k}: {v}")
        case "setup":
            if args.remove:
                print(f"skill: {advisor.remove_skill(args.force)}")
                print(f"allow rule: {advisor.remove_allow_rule()}")
                return 0
            if advisor.get_key() is None:
                advisor.prompt_key()
            if _advise_key("status", True, adv):
                return 2
            print(f"skill installed: {advisor.install_skill(args.force)}")
            print(f"allow rule: {advisor.add_allow_rule()}")
    return 0


def _advise_key(action: str, check: bool, adv: config.Advisor) -> int:
    match action:
        case "set":
            advisor.prompt_key()
        case "clear":
            print("key removed" if advisor.clear_key() else "no key stored")
        case "status":
            present = advisor.get_key() is not None
            print(f"key: {'present' if present else 'absent'}")
            if check and present:
                err = advisor.check_key(adv.model)
                print(f"{adv.model}: {'reachable' if err is None else err}")
                return 0 if err is None else 2
            if check:
                return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        run_argv = []
    elif argv[0] == "run":
        run_argv = argv[1:]
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        run_argv = argv  # bare cpool: claude flags pass through verbatim
    else:
        run_argv = None
    args = None if run_argv is not None else _parser().parse_args(argv)
    try:
        if run_argv is not None:
            from . import run

            return run.main(run_argv)
        match args.cmd:
            case "add":
                print(f"added {accounts.add(args.name, force=args.force).name}")
                _refresh_schedule()
            case "adopt":
                a = accounts.adopt(Path(args.dir).expanduser(), args.name, args.move, force=args.force)
                print(f"adopted {a.name}")
                _refresh_schedule()
            case "remove":
                accounts.remove(args.name)
                print(f"removed {args.name}")
                _refresh_schedule()
            case "list":
                return _cmd_list()
            case "status":
                return _cmd_status(args.json)
            case "switch":
                return _cmd_switch(args.session)
            case "plan":
                return _cmd_plan(args.apply)
            case "unplan":
                launchd.uninstall()
                print("launchd agents removed")
            case "warm":
                return _cmd_warm(args.due, args.account)
            case "poll":
                cfg = config.load()
                if args.daemon:
                    return usage.poll_daemon(cfg)
                usage.poll(cfg, int(time.time()))
            case "statusline":
                from . import statusline

                return statusline.main()
            case "doctor":
                return _cmd_doctor()
            case "advise":
                return _cmd_advise(args)
    except advisor.AdvisorError as e:
        print(f"cpool: {e}", file=sys.stderr)
        return e.code
    except (config.ConfigError, accounts.AccountError, CliError) as e:
        print(f"cpool: {e}", file=sys.stderr)
        return 2
    return 0
