"""cpool advise: a Fable advisor paid from API credits (docs/design/2026-11-03-advisor-design.md)."""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import journal, paths

# USD per million tokens: (input, output, cache_write_1h, cache_read)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10, 50, 20, 0.25),
    "claude-opus-5-5": (4, 20, 8, 0.20),
}

SERVICE = "cpool-advisor"
BASE_URL = "https://api.anthropic.com"
BETAS = ["server-side-fallback-2026-07-01"]
KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")
RESERVE_TTL_S = 2 * 3600
FINAL_ROUND = "This is the final round. Give a VERDICT with the facts you have and state your assumptions."

SYSTEM = """You are the senior advisor to an engineer's coding session. You cannot run tools, read files, or see the repository: everything you know is in the question, the facts, and any attached files. The session will act on your answer and can gather more facts for you with cheap read-only searches.

Answer in exactly one of two forms.

VERDICT: the decision or plan, first, in a sentence. Then the reasoning that decides it, the risks and how to contain them, and what the session should verify before or after acting. Be concrete: name files, functions, steps.

NEEDS: only when a missing fact would change the decision. List at most 7 items, each a bounded question a read-only search can answer (name the files, symbols or commands to look at) and one line on why it matters. Never ask for something the facts already give.

Prefer VERDICT whenever the facts support a sound decision; state the assumptions you made instead of asking. Be direct and dense; no preamble."""


class AdvisorError(Exception):
    code = 2


class CapError(AdvisorError):
    code = 4


# ---- key -------------------------------------------------------------------

def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["security", *args], input=stdin, capture_output=True, text=True)


def get_key() -> str | None:
    r = _security(["find-generic-password", "-s", SERVICE, "-a", getpass.getuser(), "-w"])
    key = r.stdout.strip() if r.returncode == 0 else ""
    return key or None


def set_key(key: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise AdvisorError("that doesn't look like an API key (sk-ant-...)")
    cmd = f"add-generic-password -U -s {SERVICE} -a {getpass.getuser()} -w {key}\n"
    r = _security(["-i"], stdin=cmd)
    if r.returncode != 0:
        raise AdvisorError("could not write the key to the Keychain")


def clear_key() -> bool:
    return _security(["delete-generic-password", "-s", SERVICE, "-a", getpass.getuser()]).returncode == 0


def _require_key() -> str:
    key = get_key()
    if not key:
        raise AdvisorError("no advisor key; run `cpool advise key set`")
    return key


def _client(key: str):
    import anthropic

    # Explicit api_key and base_url: the SDK then reads neither ANTHROPIC_* credentials nor ANTHROPIC_BASE_URL.
    # It still merges ANTHROPIC_CUSTOM_HEADERS after the auth header, so drop it from this process.
    os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    return anthropic.Anthropic(api_key=key, base_url=BASE_URL)


def _api_errors() -> tuple[type[BaseException], ...]:
    try:
        import anthropic

        return (anthropic.APIError,)
    except ImportError:  # pragma: no cover
        return ()


# ---- ledger and cycle -----------------------------------------------------

def _ledger() -> Path:
    return paths.advisor_dir() / "ledger.jsonl"


def _rows() -> list[dict]:
    try:
        lines = _ledger().read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _append(row: dict) -> None:
    _ledger().parent.mkdir(parents=True, exist_ok=True)
    with open(_ledger(), "a") as f:
        f.write(json.dumps(row) + "\n")


def cycle_start(cycle_day: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    y, m = now.year, now.month
    if now.day < cycle_day:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return datetime(y, m, cycle_day)


def cycle(adv, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    start = cycle_start(adv.cycle_day, datetime.fromtimestamp(now)).timestamp()
    rows = _rows()
    finals = {r.get("id") for r in rows if r.get("kind") == "final"}
    spent = calls = 0.0
    open_res = []
    for r in rows:
        if r.get("kind") == "final" and r.get("ts", 0) >= start:
            spent += r.get("cost_usd", 0)
            calls += 1
        elif r.get("kind") == "reserve" and r.get("id") not in finals and now - r.get("ts", 0) < RESERVE_TTL_S:
            open_res.append(r)
    spent += sum(r.get("worst", 0) for r in open_res)
    return {"start": start, "spent": spent, "cap": adv.monthly_cap_usd, "calls": int(calls), "open": len(open_res)}


def _price(model: str) -> tuple[float, float, float, float]:
    # an unknown model (a fallback rescue) is charged at the highest price: safe for a cap
    return PRICES.get(model) or max(PRICES.values(), key=lambda p: p[1])


def _u(usage, name: str) -> int:
    v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(v or 0)


def cost(model: str, usage) -> float:
    pin, pout, pcw, pcr = _price(model)
    return (_u(usage, "input_tokens") * pin + _u(usage, "output_tokens") * pout
            + _u(usage, "cache_creation_input_tokens") * pcw + _u(usage, "cache_read_input_tokens") * pcr) / 1e6


# ---- threads --------------------------------------------------------------

def _thread_path(tid: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", tid):
        raise AdvisorError(f"bad thread id: {tid}")
    return paths.advisor_dir() / "threads" / f"{tid}.json"


def _new_thread(model: str) -> dict:
    tid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    return {"id": tid, "model": model, "created": int(time.time()), "messages": []}


def _load_thread(tid: str) -> dict:
    try:
        return json.loads(_thread_path(tid).read_text())
    except FileNotFoundError:
        raise AdvisorError(f"no such thread: {tid}") from None


def _save_thread(t: dict) -> None:
    p = _thread_path(t["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(t))
    os.replace(tmp, p)


def _block_dict(b) -> dict:
    return b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)


# ---- answer parsing -------------------------------------------------------

_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*)$")


def parse(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("NEEDS:"):
            items = [m.group(1).strip() for m in map(_ITEM.match, lines[i + 1:]) if m]
            return "needs", items
    if any(ln.lstrip().startswith("VERDICT:") for ln in lines):
        return "verdict", []
    return "unstructured", []


# ---- ask ------------------------------------------------------------------

def _user_content(question: str, facts: Path, attach: list[Path], limit: int) -> str:
    try:
        sizes = {str(facts): facts.stat().st_size} | {str(p.resolve()): p.stat().st_size for p in attach}
    except OSError as e:
        raise AdvisorError(f"cannot read {e.filename}") from None
    total = sum(sizes.values())
    if total > limit:
        listed = ", ".join(f"{k}={v}" for k, v in sizes.items())
        raise AdvisorError(f"facts and attachments are {total} bytes, over max_attach_bytes={limit} ({listed})")
    try:
        parts = [f"<question>{question}</question>", f"<facts>{facts.read_text()}</facts>"]
        for p in attach:
            parts.append(f'<file path="{p.resolve()}">{p.read_text()}</file>')
    except (OSError, UnicodeDecodeError) as e:
        raise AdvisorError(f"cannot read a facts or attach file: {e}") from None
    return "\n".join(parts)


def ask(adv, question: str, facts: Path, attach: list[Path], thread: str = "new",
        model: str | None = None, effort: str | None = None) -> tuple[int, dict]:
    if not question.strip():
        raise AdvisorError("no question")
    if model is not None and model not in PRICES:
        raise AdvisorError(f"unknown model {model}; one of: {', '.join(PRICES)}")
    if thread == "new":
        t = _new_thread(model or adv.model)
    else:
        t = _load_thread(thread)
        if model is not None and model != t["model"]:
            raise AdvisorError(f"thread {t['id']} uses {t['model']}; --model can't change it")
    model, effort = t["model"], effort or adv.effort
    rnd = sum(1 for m in t["messages"] if m["role"] == "user") + 1
    if rnd > adv.max_rounds:
        raise AdvisorError(f"thread {t['id']} has used all {adv.max_rounds} rounds")
    content = _user_content(question, facts, attach, adv.max_attach_bytes)
    if rnd == adv.max_rounds:
        content += "\n" + FINAL_ROUND
    messages = t["messages"] + [{"role": "user", "content": content}]
    system = [{"type": "text", "text": SYSTEM}]
    client = _client(_require_key())
    errs = _api_errors()
    pout, pcw = _price(model)[1], _price(model)[2]

    rid = secrets.token_hex(8)
    paths.advisor_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.advisor_dir() / "ledger.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            in_tok = _u(client.messages.count_tokens(model=model, system=system, messages=messages), "input_tokens")
        except errs as e:
            raise AdvisorError(f"{type(e).__name__}: {e}") from None
        worst = (in_tok * pcw + adv.max_tokens * pout) / 1e6
        cyc = cycle(adv)
        if cyc["spent"] + worst > adv.monthly_cap_usd:
            raise CapError(f"spent ${cyc['spent']:.2f} of ${adv.monthly_cap_usd:.2f} this cycle; "
                           f"this call could cost up to ${worst:.2f}")
        _append({"ts": time.time(), "kind": "reserve", "id": rid, "thread": t["id"], "round": rnd, "worst": worst})
    finally:
        os.close(fd)

    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=adv.max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral", "ttl": "1h"},
            betas=BETAS,
            fallbacks="default",
        ) as stream:
            resp = stream.get_final_message()
    except errs as e:
        _append({"ts": time.time(), "kind": "final", "id": rid, "cost_usd": 0, "error": type(e).__name__})
        raise AdvisorError(f"{type(e).__name__}: {e}") from None

    usage = resp.usage
    c = cost(resp.model, usage)
    u = {k: _u(usage, k) for k in ("input_tokens", "output_tokens",
                                   "cache_creation_input_tokens", "cache_read_input_tokens")}
    _append({"ts": time.time(), "kind": "final", "id": rid, "thread": t["id"], "round": rnd, "model": resp.model,
             "stop_reason": resp.stop_reason, **u, "cost_usd": c})
    journal.log("advise", model=resp.model, thread=t["id"], round=rnd, cost_usd=c, **u)

    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    cyc = cycle(adv)
    result = {"status": "refusal", "text": text, "needs": [], "thread": t["id"], "round": rnd,
              "max_rounds": adv.max_rounds, "model": resp.model, "cost_usd": c,
              "cycle_spent_usd": cyc["spent"], "cap_usd": adv.monthly_cap_usd,
              "truncated": resp.stop_reason == "max_tokens", "usage": u}
    if resp.stop_reason == "refusal":
        sd = getattr(resp, "stop_details", None)
        result["stop_details"] = _block_dict(sd) if sd is not None else None
        return 5, result
    t["messages"] = messages + [{"role": "assistant", "content": [_block_dict(b) for b in resp.content]}]
    _save_thread(t)
    result["status"], result["needs"] = parse(text)
    return (3 if result["status"] == "needs" else 0), result


# ---- skill ----------------------------------------------------------------

def skill_text() -> str:
    return resources.files("cpool").joinpath("assets/fable/SKILL.md").read_text()


def skill_target() -> Path:
    shared = paths.shared_dir() / "skills"
    base = shared if shared.is_dir() else Path.home() / ".claude" / "skills"
    return base / "fable" / "SKILL.md"


def install_skill(force: bool = False) -> Path:
    dst, text = skill_target(), skill_text()
    if dst.exists() and dst.read_text() != text and not force:
        raise AdvisorError(f"{dst} exists and differs; rerun with --force to replace it")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    return dst


def check_key(model: str) -> str | None:
    """None when the key reaches `model`, else the error."""
    try:
        _client(_require_key()).models.retrieve(model)
        return None
    except AdvisorError as e:
        return str(e)
    except _api_errors() as e:
        return f"{type(e).__name__}: {e}"


def prompt_key() -> None:
    key = getpass.getpass("Anthropic API key: ").strip()
    set_key(key)
    print("key stored in the Keychain", file=sys.stderr)
