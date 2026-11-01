# cpool contract (2026-11-01)

Companion to `2026-11-01-cpool-design.md`. Fixes the persisted formats, env vars and module signatures every wave-1 worker builds against. Fields marked **(S4)** are confirmed or renamed once spike S4 reports; nothing else moves without a design change.

## Package layout

```
cpool/
  pyproject.toml            [project.scripts] cpool = "cpool.cli:main"; requires-python >=3.12; no deps
  src/cpool/
    __init__.py             __version__
    paths.py                home(), config_path(), state_path(), handoff_dir(), log_dir(), shared_dir(), accounts_dir()
    config.py               Config, Account, load()
    state.py                read(), locked(), account_state(), session_state()
    journal.py              log(kind, **fields)
    accounts.py             W1  keychain_service(), launch_env(), storage_dir(), resolve_account(), add(), adopt(), remove()
    mirror.py               W1  SHARED, PER_ACCOUNT, link(), check()
    router.py               W2  Decision, score(), choose()
    statusline.py           W3  main()  (stdin JSON → state, then chain)
    usage.py                W3  token(), fetch(), poll(), poll_daemon()
    schedule.py             W4  WarmSlot, plan(), due()
    warm.py                 W4  warm(), warm_due()
    launchd.py              W4  install(), uninstall(), pmset_apply()
    handoff.py              W6  Handoff, write(), take()
    run.py                  W6  main()
    cli.py                  W6  main()
  plugin/                   W5  .claude-plugin/plugin.json, hooks/{hooks.json,register.ts,*.test.ts}, types/
  tests/
```
Skeleton (wave 0b) implements `paths`, `config`, `state`, `journal` fully and stubs the rest with these signatures.

## Env vars

| var | set by | meaning |
|---|---|---|
| `CPOOL_HOME` | user/tests | overrides `~/.cpool` |
| `CLAUDE_CONFIG_DIR` | `run`, `warm` | the account's `dir` launch string, ~-expanded, no trailing `/` (Keychain hash is of this exact string); unset for `dir = "default"` |
| `CPOOL_ACCOUNT` | `run` | account name, for the mod and statusline |
| `CPOOL_RUN` | `run` | uuid4 for one `cpool run` invocation; stable across relaunches; keys the handoff file |
| `CPOOL_STATUSLINE_NEXT` | `add`/`doctor` writes into shared settings | the user's original statusline command, chained after ours |

## `~/.cpool/config.toml`

```toml
[pool]
start_hour   = 8      # first reset of account order=1
soft         = 85     # u5h at/above → leave at next Stop
projected    = 97     # u5h + est_next_turn at/above → leave
weekly_soft  = 90     # u7d at/above → ineligible
margin       = 10     # hysteresis for moving a non-forced session
cooldown_min = 5      # an account just left is ineligible this long
stale_min    = 20     # usage older than this gets -20
poll_min     = 10
jitter_s     = [5, 15]
ctx_max_uncompacted = 30   # never switch an uncompacted context above this % (compact first)

[[account]]
name    = "main"
dir     = "default"                  # launch string: "default" = CLAUDE_CONFIG_DIR unset; else ~-expanded absolute path, e.g. "~/.claude2"
order   = 1                          # stagger slot
enabled = true
```
Validation: names `[a-z0-9-]+`, unique; orders unique; dirs unique by realpath. Missing file → empty pool (not an error).

## `~/.cpool/state.json`

Written only under `state.locked()` (fcntl.flock on `state.json.lock`, write temp + `os.replace`). Unknown keys preserved. Readers tolerate missing keys.

```json
{
  "version": 1,
  "accounts": {
    "main": {
      "five_hour":  {"used": 42.0, "resets_at": 1762020000},
      "seven_day":  {"used": 11.0, "resets_at": 1762500000},
      "model_week": {"opus": {"used": 30.0, "resets_at": 1762500000}},   // from seven_day_opus / seven_day_sonnet; null windows omitted
      "credits_enabled": false,          // extra_usage.is_enabled OR spend.enabled; null = unknown → ineligible
      "tier": "default_claude_max_5x",   // Keychain claudeAiOauth.rateLimitTier (read-only, non-secret)
      "source": "statusline",            // "statusline" | "poll"
      "updated_at": 1762000000,
      "auth": "ok",                      // "ok" | "expired" | "missing"
      "left_at": null,                   // epoch when a session last moved off (cooldown)
      "limited": {"model": null, "until": null},   // from a limit banner/exit: model name or "all"
      "poll_backoff_until": null
    }
  },
  "sessions": {
    "<session_id>": {
      "account": "main", "run": "<CPOOL_RUN|null>", "pid": 4242, "cwd": "/abs",
      "model": "claude-opus-5-5", "started_at": 1762000000, "updated_at": 1762000100,
      "ctx_used": 18.0,
      "turns": [{"ts": 1762000100, "u5h": 41.0}],     // last 6 kept; est_next_turn = median of deltas
      "cache": {"read": 0, "created": 0}              // from prompt_cache, last turn
    }
  }
}
```
Usage endpoint (S4, verified): `GET https://api.anthropic.com/api/oauth/usage`, headers `Authorization: Bearer`, `anthropic-beta: oauth-2025-04-20`, a User-Agent. Window objects are `{utilization: 0-100 float, resets_at: ISO8601 | null, ...}`; `usage.fetch` converts `utilization`→`used` and `resets_at`→epoch int. `limits[]` (`kind: session|weekly_all|weekly_scoped`, `scope.model`) is kept raw under `accounts.<n>.limits` for display only. Obfuscated top-level keys are ignored.
Capacity: percentages are not comparable across tiers. `config.Account.capacity` (float) defaults from `tier`: `default_claude_ai` (Pro) 1, `*_max_5x` 5, `*_max_20x` 20, unknown 1; overridable in config. The router works in capacity units (see router note below).
A window whose `resets_at` has passed reads as `{"used": 0, "resets_at": null}`. A session whose `pid` is dead is pruned on every `locked()` write.

## `~/.cpool/handoff/<CPOOL_RUN>.json`

Written by the mod (W5), consumed (read + delete) by `run` (W6).
```json
{"version": 1, "run": "<uuid>", "session_id": "<id>", "from": "main", "model": "claude-opus-5-5",
 "reason": "soft|projected|hard|model", "compacted": true, "ts": 1762000200}
```
No file on child exit → `run` exits with the child's code, except the hard-limit path (W6 detects via `state.accounts[from].limited` set by the statusline/banner, then synthesizes `reason: "hard", compacted: false`).

## `~/.cpool/log/<YYYY-MM-DD>.jsonl`

One object per line: `{"ts": epoch, "kind": "route|switch|warm|poll|cache_miss|adopt|error", ...}`. Never a token, never a credential field.

## Signatures

```python
# paths.py
def home() -> Path                       # $CPOOL_HOME or ~/.cpool
# config.py
@dataclass(frozen=True) class Account: name: str; dir: Path; order: int; enabled: bool = True; capacity: float | None = None   # None → from state tier
@dataclass(frozen=True) class Pool: start_hour:int=8; soft:float=85; projected:float=97; weekly_soft:float=90; margin:float=10; cooldown_min:int=5; stale_min:int=20; poll_min:int=10; jitter_s:tuple[int,int]=(5,15); ctx_max_uncompacted:float=30
@dataclass(frozen=True) class Config: pool: Pool; accounts: tuple[Account, ...]
def load(path: Path | None = None) -> Config
def save_account(acct: Account) -> None   # append/replace one [[account]], keep the rest of the file
# state.py
def read() -> dict
@contextmanager def locked() -> Iterator[dict]   # mutate in place; written on clean exit
# journal.py
def log(kind: str, **fields) -> None
# accounts.py
def keychain_service(config_dir: str | None) -> str   # None → "Claude Code-credentials"; else "...-" + sha256(config_dir)[:8]
def launch_env(acct: Account) -> dict[str, str | None]   # {'CLAUDE_CONFIG_DIR': str} or {'CLAUDE_CONFIG_DIR': None} (= unset) for default
def storage_dir(acct: Account) -> Path                    # realpath of the launch dir (default → ~/.claude)
def resolve_account(cfg: Config, config_dir: str | None) -> Account | None   # by realpath of storage_dir; None → the default account
def add(name: str) -> Account                         # mkdir, mirror.link, interactive `claude /login`
def adopt(src: Path, name: str, move: bool) -> Account
def remove(name: str) -> None                         # deregister; never deletes credentials or the dir
# mirror.py
SHARED: tuple[str, ...]      # projects, settings.json, CLAUDE.md, skills, agents, plugins, commands, hooks, history.jsonl
PER_ACCOUNT: tuple[str, ...] # .claude.json, daemon, ide, .credentials.json
def link(acct_dir: Path) -> list[str]   # idempotent; never replaces a real non-empty entry without merging into shared first
def check(acct_dir: Path) -> list[str]  # problems, for doctor
# router.py  (pure: no I/O)
@dataclass(frozen=True) class Decision: account: str | None; scores: dict[str, float]; reason: str; earliest_reset: int | None
def score(name: str, acct: dict, sessions: dict, *, cwd: str, now: int, pool: Pool) -> float | None   # None = ineligible
def choose(state: dict, cfg: Config, *, model: str, cwd: str, now: int, current: str | None = None, exclude: frozenset[str] = frozenset(), forced: bool = False) -> Decision
# usage.py
def token(acct: Account) -> str | None   # read-only Keychain read; never logged
def fetch(acct: Account) -> dict          # normalized to the state.accounts[name] shape; raises UsageError(status)
def poll(cfg: Config, now: int) -> None   # skips accounts with a live session or in backoff
# statusline.py
def main() -> int                         # stdin → state; then exec CPOOL_STATUSLINE_NEXT with the same stdin; prints its output
# schedule.py
@dataclass(frozen=True) class WarmSlot: account: str; hour: int; minute: int
def plan(cfg: Config) -> list[WarmSlot]
def due(cfg: Config, state: dict, now: datetime) -> list[str]   # slot passed <1h ago, account has no open 5h window
# warm.py
def warm(acct: Account) -> bool           # CLAUDE_CONFIG_DIR=<dir> claude -p --model haiku "ok"
# handoff.py
def write(h: dict) -> Path ; def take(run: str) -> dict | None
# run.py
def main(argv: list[str]) -> int          # route → spawn → on exit take handoff → jitter → relaunch --resume
```

## Ownership (wave 1)

W1 accounts+mirror · W2 router (+tests) · W3 statusline+usage · W4 schedule+warm+launchd · W5 plugin/. Skeleton files (`paths`, `config`, `state`, `journal`, `pyproject.toml`) are read-only to wave 1; W6 owns `cli.py`, `run.py`, `handoff.py`, `pyproject.toml` in wave 2.

## Wave-1 decisions (2026-11-01)

- Leave logic has one source: `router.should_leave(sess, acct, pool, now) -> "soft"|"projected"|"model"|None`. The statusline calls it on every update and stores `sessions.<sid>.leave`; the mod only reads that field at `turn.complete`.
- `cpool run` loads the mod with `--plugin-dir <pkg>/plugin` and the statusline with `--settings '{"statusLine":{"type":"command","command":"cpool statusline"}}'`, exporting the user's own statusline command as `CPOOL_STATUSLINE_NEXT`. No global settings are changed; sessions outside `cpool run` are covered by the poller.
- Stagger step is in 30-min slots: `k = max(30, floor30(300/N))`; N=2, S=8 → main warms 03:02 (resets 08/13/18/23:00), claude2 05:32 (10:30/15:30/20:30/01:30). Warm-ups only at the 4 daily slots per account.
- `pmset` needs sudo: cpool prints the command, never runs it.
- Manual switch: `cpool switch` sets `sessions.<sid>.leave_manual = true`; the mod treats it as reason "manual"; the statusline clears it only when the session shows up on a different account.
- Known limit: the mod acts only at `turn.complete` (main loop; subagent and engine-fork turns, i.e. `agentId` set, are ignored). A handoff whose file write failed retries on the next turn, so an idle headless session stalls until prompted.
- `cpool run` passes the router a state copy without the session's own record, so a session doesn't bias its current account (+10 affinity, −3 load).
