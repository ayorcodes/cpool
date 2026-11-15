from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from . import accounts, advisor, chain, codex, config, journal, launchd, mirror, render, router, schedule, shard, shardacct, state, usage, warm


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
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true")
    mode.add_argument("--due", action="store_true")

    sub.add_parser("statusline", help="statusline wrapper (reads stdin)")
    sub.add_parser("run", help="run claude on the best account: cpool run [claude args]")
    sub.add_parser("doctor", help="check the setup")

    s = sub.add_parser("shard", help="route new sessions through the shard proxy")
    sh = s.add_subparsers(dest="shard_cmd", required=True)
    sh.add_parser("on", help="enable shard routing for new sessions")
    sh.add_parser("off", help="disable shard routing for new sessions")
    sh.add_parser("status", help="show shard routing and proxy availability")
    _shard_mgmt_parser(sh)

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
    live, _ = shardacct.live_by_account(cfg, st, now)
    rows = []
    for a in cfg.accounts:
        if not a.enabled and not live[a.name]:
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
                "sessions": live[a.name],
                "source": acct.get("source") or "-",
                "age": _age(acct.get("updated_at"), now),
            }
        )
    return rows


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
    snap = codex.snapshot(now)
    if as_json:
        outlook = chain.summary(cfg, st, now)
        print(json.dumps({"accounts": rows, "codex": snap,
                          "pool": asdict(outlook) if outlook is not None else None,
                          "next": {"account": dec.account, "reason": dec.reason}}, indent=2))
        return 0
    color = render.color_enabled(sys.stdout)
    active = accounts.resolve_account(cfg, os.environ.get("CLAUDE_CONFIG_DIR"))
    name_width = max(len("codex") if snap is not None else 0,
                     max((len(r["name"]) for r in rows), default=0))
    name_width = max(name_width, 4)
    stored = st.get("accounts") or {}
    header, serving = render.shard_header(cfg, st, now)
    if header is not None:
        print(header)
    serving = serving if header is not None else None
    lines, plains, extras = [], [], []
    for r in rows:
        acct = stored.get(r["name"]) or {}
        is_active = r["name"] in serving if serving is not None else active is not None and r["name"] == active.name
        lines.append(render.account_line(r["name"], acct, now, active=is_active, name_width=name_width, color=color, serving=serving,
                                         live=r["sessions"], stale_after_s=3 * cfg.pool.live_poll_s))
        plains.append(render.account_line(r["name"], acct, now, active=is_active, name_width=name_width, color=False, serving=serving,
                                          live=r["sessions"], stale_after_s=3 * cfg.pool.live_poll_s))
        age = "just now" if r["age"] == "0m" else f"{r['age']} ago"
        seen = "no data" if r["source"] == "-" else f"{r['source']} {age}"
        mw = "  ".join(f"{f} {w['used']:.0f}% {render.until(w['resets_at'], now)}".rstrip()
                       for f, w in r["model_week"].items())
        credits = "credits on" if r["credits"] == "ON" else ""
        sessions = f"{r['sessions']} session" + ("" if r["sessions"] == 1 else "s") if r["sessions"] else ""
        extras.append([r["tier"], sessions, seen, mw, credits])
    widths = [max((len(e[i]) for e in extras), default=0) for i in range(5)]
    gutter = max((len(p) for p in plains), default=0) + 3
    for line, plain, cols in zip(lines, plains, extras):
        tail = "  ".join(f"{c:<{w}}" for c, w in zip(cols, widths) if w).rstrip()
        print(line + " " * (gutter - len(plain)) + render.paint(tail, render.DIM, on=color))
    if snap is not None:
        print(render.codex_line(snap, now, name_width=name_width, color=color))
    for line in render.pool_lines(cfg, st, now, name_width=name_width, color=color):
        print(line)
    pick = render.paint(dec.account or "none", render.BOLD, on=color and dec.account is not None)
    print(render.paint("→ next:", render.DIM, on=color) + f" {pick} " + render.paint(f"({dec.reason})", render.DIM, on=color))
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
    st = state.read()
    slots = schedule.plan(cfg, st)
    if not slots:
        print("no enabled accounts")
    for s in slots:
        print(f"{s.account}: warm {s.hour:02d}:{s.minute:02d}, resets {' '.join(_resets(s))}")
    if cfg.pool.warm == "chain":
        print("chain: re-warms each idle account at its own reset (checked every 5 min)")
    live = {a.name for a in schedule.active(cfg, st)}
    idle = [a.name for a in cfg.accounts if a.enabled and a.name not in live]
    if idle:
        print(f"inactive (no warm slots): {', '.join(idle)}")
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


def _replan_if_active_changed(cfg: config.Config) -> None:
    names = [a.name for a in schedule.active(cfg, state.read())]
    previous = state.read().get("plan_active")
    if previous == names:
        return
    warm_plist = Path.home() / "Library" / "LaunchAgents" / f"{launchd.WARM}.plist"
    if previous is not None and warm_plist.exists():
        launchd.install(cfg, _cpool_bin(), only=(launchd.WARM,))
        journal.log("replan", active=names)
    with state.locked() as st:
        st["plan_active"] = names


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
    if cfg.shard.enabled:
        out(shard.probe(cfg.shard.url, shard.key()), f"shard proxy reachable: {cfg.shard.url}")
        key = shardacct.get_key()
        if not key:
            out(False, "shard management key: absent (cpool shard mgmt-key set)")
        else:
            try:
                cfg_json = shardacct.mgmt("/v0/management/config", key)
                routing = cfg_json.get("routing") or {}
                strategy = routing.get("strategy")
                affinity = routing.get("session-affinity")
                out(strategy == "fill-first", f"cliproxyapi routing.strategy: fill-first (is {strategy or 'unset'})")
                out(affinity is True, f"cliproxyapi routing.session-affinity: true (is {affinity})")
                if strategy != "fill-first" or affinity is not True:
                    print("set these in the cliproxyapi config; cpool does not edit it")
            except Exception as error:
                out(False, f"shard management API: {error}")
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


def _shard_mgmt_parser(subparsers) -> None:
    parser = subparsers.add_parser("mgmt-key", help="manage the cliproxyapi management key")
    parser.add_argument("action", choices=["set", "clear", "status"])
    parser.add_argument("--check", action="store_true", help="explicitly check once, even after rejection")


def _shard_mgmt_key(action: str, check: bool) -> int:
    match action:
        case "set":
            try:
                shardacct.prompt_key()
            except ValueError as error:
                raise CliError(str(error)) from None
        case "clear":
            print("key removed" if shardacct.clear_key() else "no key stored")
        case "status":
            present = shardacct.get_key() is not None
            source = "env" if os.environ.get("CPOOL_SHARD_MGMT_KEY", "").strip() else "keychain"
            print(f"present ({source})" if present else "absent")
            if error := shardacct.cached()["error"]:
                print(f"last error: {error}")
            if check:
                result = shardacct.check_auth(time.time()) if present else None
                if result is None:
                    return 2
                match result["error"]:
                    case None:
                        print(f"ok: {len(result['map'])} auths")
                        return 0
                    case "auth":
                        remaining = max(0, 5 - result["failures"])
                        print(f"401: key rejected ({remaining} more failures lock management for 30 min)")
                    case "down":
                        print("management unavailable")
                return 2
    return 0


def _cmd_shard(action: str) -> int:
    settings = config.load().shard
    match action:
        case "on":
            proxy_key = shard.key()
            if proxy_key is None:
                print("cpool: shard requires CPOOL_SHARD_KEY or CLI_PROXY_API_KEY", file=sys.stderr)
                return 1
            if not shard.probe(settings.url, proxy_key):
                print(f"cpool: shard proxy unreachable: {settings.url}", file=sys.stderr)
                return 1
            config.save_shard(replace(settings, enabled=True))
            print(f"shard on: new cpool run sessions route via {settings.url}; running sessions unchanged")
        case "off":
            config.save_shard(replace(settings, enabled=False))
            print("shard off: new cpool run sessions run unsharded; running sessions unchanged")
        case "status":
            proxy_key = shard.key()
            print(f"shard: {'enabled' if settings.enabled else 'disabled'}")
            print(f"url: {settings.url}")
            print(f"key: {'present' if proxy_key is not None else 'absent'}")
            print(f"reachable: {'yes' if shard.probe(settings.url, proxy_key) else 'no'}")
            print(f"live shard sessions: {state.live_shard_sessions(state.read())}")
            base_url = os.environ.get("ANTHROPIC_BASE_URL")
            if base_url and shard.relay_supports_shard(base_url):
                print(f"route: via relay {base_url}")
            else:
                print(f"route: direct {settings.url}")
                if base_url is not None and base_url != settings.url:
                    print("warning: relay/lite route is overridden by shard")
    return 0


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
                for name in advisor.SKILLS:
                    print(f"skill {name}: {advisor.remove_skill(name, args.force)}")
                print(f"allow rule: {advisor.remove_allow_rule()}")
                return 0
            if advisor.get_key() is None:
                advisor.prompt_key()
            if _advise_key("status", True, adv):
                return 2
            for dst in advisor.install_skills(args.force):
                print(f"skill installed: {dst}")
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
                usage.poll(cfg, int(time.time()), due=args.due)
                if args.due:
                    return 0
                try:
                    warm.warm_due(cfg)
                except Exception as e:  # noqa: BLE001
                    journal.log("error", where="cli.poll.warm", error=type(e).__name__)
                try:
                    _replan_if_active_changed(cfg)
                except Exception as e:  # noqa: BLE001
                    journal.log("error", where="cli.poll.replan", error=type(e).__name__)
            case "statusline":
                from . import statusline

                return statusline.main()
            case "doctor":
                return _cmd_doctor()
            case "shard":
                return _shard_mgmt_key(args.action, args.check) if args.shard_cmd == "mgmt-key" else _cmd_shard(args.shard_cmd)
            case "advise":
                return _cmd_advise(args)
    except advisor.AdvisorError as e:
        print(f"cpool: {e}", file=sys.stderr)
        return e.code
    except (config.ConfigError, accounts.AccountError, CliError) as e:
        print(f"cpool: {e}", file=sys.stderr)
        return 2
    return 0
