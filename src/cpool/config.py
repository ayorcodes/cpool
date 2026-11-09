"""config.toml load/save."""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import paths


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    dir: Path | None  # None = default account: launch with CLAUDE_CONFIG_DIR unset
    order: int
    enabled: bool = True
    capacity: float | None = None


@dataclass(frozen=True)
class Pool:
    start_hour: int = 8
    soft: float = 85
    projected: float = 97
    weekly_soft: float = 90
    expiry_h: float = 36
    expiry_weight: float = 100
    margin: float = 10
    cooldown_min: int = 5
    stale_min: int = 20
    poll_min: int = 10
    jitter_s: tuple[int, int] = (5, 15)
    ctx_max_uncompacted: float = 30


@dataclass(frozen=True)
class Advisor:
    model: str = "claude-fable-5-2"
    effort: str = "high"
    max_tokens: int = 32000
    monthly_cap_usd: float = 100.0
    cycle_day: int = 1
    max_rounds: int = 3
    max_attach_bytes: int = 2000000


@dataclass(frozen=True)
class Config:
    pool: Pool
    accounts: tuple[Account, ...]
    advisor: Advisor = field(default_factory=Advisor)


_NAME_RE = re.compile(r"[a-z0-9-]+")


def _expand(p: str) -> Path:
    path = Path(os.path.expanduser(p))
    if not path.is_absolute():
        raise ConfigError(f"dir {p!r} must be an absolute path (or start with ~)")
    return path


def _parse_pool(raw: dict) -> Pool:
    known = {f.name for f in fields(Pool)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown [pool] keys: {', '.join(sorted(unknown))}")
    kw = dict(raw)
    if "jitter_s" in kw:
        j = kw["jitter_s"]
        if (
            not isinstance(j, list)
            or len(j) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in j)
            or j[0] > j[1]
            or j[0] < 0
        ):
            raise ConfigError("pool.jitter_s must be [min, max] non-negative ints")
        kw["jitter_s"] = (j[0], j[1])
    for k, v in kw.items():
        if k != "jitter_s" and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise ConfigError(f"pool.{k} must be a number")
    return Pool(**kw)


_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _parse_advisor(raw: dict) -> Advisor:
    from .advisor import PRICES

    if not isinstance(raw, dict):
        raise ConfigError("[advisor] must be a table")
    known = {f.name for f in fields(Advisor)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown [advisor] keys: {', '.join(sorted(unknown))}")

    def num(k: str, lo: float, hi: float, ints: bool = True):
        v = raw.get(k)
        if v is None:
            return getattr(Advisor, k)
        ok_type = isinstance(v, int) if ints else isinstance(v, (int, float))
        if isinstance(v, bool) or not ok_type or not lo <= v <= hi:
            raise ConfigError(f"advisor.{k} must be {'an integer' if ints else 'a number'} in [{lo}, {hi}]")
        return v

    model = raw.get("model", Advisor.model)
    if model not in PRICES:
        raise ConfigError(f"advisor.model must be one of: {', '.join(PRICES)}")
    effort = raw.get("effort", Advisor.effort)
    if effort not in _EFFORTS:
        raise ConfigError(f"advisor.effort must be one of: {', '.join(_EFFORTS)}")
    cap = raw.get("monthly_cap_usd", Advisor.monthly_cap_usd)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        raise ConfigError("advisor.monthly_cap_usd must be a number > 0")
    return Advisor(
        model=model,
        effort=effort,
        max_tokens=num("max_tokens", 1, 10**7),
        monthly_cap_usd=float(cap),
        cycle_day=num("cycle_day", 1, 28),
        max_rounds=num("max_rounds", 1, 6),
        max_attach_bytes=num("max_attach_bytes", 1, 10**10),
    )


def _parse_account(raw: dict, idx: int) -> Account:
    where = f"account #{idx + 1}"
    for key in ("name", "dir", "order"):
        if key not in raw:
            raise ConfigError(f"{where}: missing '{key}'")
    name = raw["name"]
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ConfigError(f"{where}: name {name!r} must match [a-z0-9-]+")
    if not isinstance(raw["dir"], str) or not raw["dir"]:
        raise ConfigError(f"account {name}: dir must be a non-empty string")
    order = raw["order"]
    if isinstance(order, bool) or not isinstance(order, int):
        raise ConfigError(f"account {name}: order must be an integer")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"account {name}: enabled must be a boolean")
    cap = raw.get("capacity")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0):
        raise ConfigError(f"account {name}: capacity must be a positive number")
    d = None if raw["dir"] == "default" else _expand(raw["dir"])
    return Account(name, d, order, enabled, float(cap) if cap is not None else None)


def _validate(accounts: list[Account]) -> None:
    names: set[str] = set()
    orders: set[int] = set()
    dirs: set[str] = set()
    defaults = [a.name for a in accounts if a.dir is None]
    if len(defaults) > 1:
        raise ConfigError(f"at most one default account, found: {', '.join(defaults)}")
    for a in accounts:
        if a.name in names:
            raise ConfigError(f"duplicate account name: {a.name}")
        if a.order in orders:
            raise ConfigError(f"duplicate account order: {a.order}")
        real = os.path.realpath(os.path.expanduser("~/.claude") if a.dir is None else a.dir)
        if real in dirs:
            raise ConfigError(f"duplicate account dir: {a.dir}")
        names.add(a.name)
        orders.add(a.order)
        dirs.add(real)


def load(path: Path | None = None) -> Config:
    path = path or paths.config_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return Config(Pool(), ())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    pool = _parse_pool(data.get("pool", {}))
    raw_accts = data.get("account", [])
    if not isinstance(raw_accts, list):
        raise ConfigError("[[account]] must be an array of tables")
    accounts = [_parse_account(r, i) for i, r in enumerate(raw_accts)]
    _validate(accounts)
    return Config(pool, tuple(accounts), _parse_advisor(data.get("advisor", {})))


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return _q(v)
    if isinstance(v, (tuple, list)):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def _dump(cfg: Config) -> str:
    out = ["[pool]"]
    for f in fields(Pool):
        out.append(f"{f.name} = {_val(getattr(cfg.pool, f.name))}")
    for a in cfg.accounts:
        out += ["", "[[account]]", f"name = {_q(a.name)}", f"dir = {_q('default' if a.dir is None else str(a.dir))}",
                f"order = {a.order}", f"enabled = {_val(a.enabled)}"]
        if a.capacity is not None:
            out.append(f"capacity = {_val(a.capacity)}")
    if cfg.advisor != Advisor():  # keep a user's [advisor] section across account writes
        out += ["", "[advisor]"] + [f"{f.name} = {_val(getattr(cfg.advisor, f.name))}" for f in fields(Advisor)]
    return "\n".join(out) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _lock() -> int:
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.with_name("config.toml.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def save_account(acct: Account) -> None:
    fd = _lock()
    try:
        cfg = load()
        accounts = list(cfg.accounts)
        for i, a in enumerate(accounts):
            if a.name == acct.name:
                accounts[i] = acct
                break
        else:
            accounts.append(acct)
        _validate(accounts)
        _write_atomic(paths.config_path(), _dump(Config(cfg.pool, tuple(accounts), cfg.advisor)))
    finally:
        os.close(fd)


def remove_account(name: str) -> None:
    fd = _lock()
    try:
        cfg = load()
        accounts = [a for a in cfg.accounts if a.name != name]
        if len(accounts) == len(cfg.accounts):
            raise ConfigError(f"no such account: {name}")
        _write_atomic(paths.config_path(), _dump(Config(cfg.pool, tuple(accounts), cfg.advisor)))
    finally:
        os.close(fd)
