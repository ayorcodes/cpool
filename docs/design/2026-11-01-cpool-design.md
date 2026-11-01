# cpool design (2026-11-01)

Status: proposed. Size: large. Stakes: sensitive (persisted state format, credential-adjacent).

## Goal

Pool N personal Claude subscriptions (Pro today) so Claude Code sessions always have usage:
route each session to the best account, stagger 5h windows so resets are spread across the day,
and move a session to another account (compact first) before its account runs out.
Standalone; no dependency on cadre. Cadre opts in by setting its role command to `cpool run --`.

## Non-goals

- No proxy, gateway or relay. No request is ever sent by cpool on a model's behalf except warm-ups via the official `claude` CLI.
- No credential copying, refreshing, export or `/logout`. Each account's login is owned by Claude Code in that account's dir.
- No usage-credit (overage) billing: an account with usage credits on is never routed to.
- No Linux/Windows in v1 (macOS Keychain, launchd, pmset).

## Prior art (2026-11-01)

| tool | takes | leaves |
|---|---|---|
| cc-pool (yasyf, Go) | per-account `CLAUDE_CONFIG_DIR`, symlink mirror of `~/.claude` minus `daemon/` `ide/`, scoring formula, `/api/oauth/usage` poll | start-time only, no mid-session move |
| claude-swap (Python) | 90% threshold, hysteresis, cooldown, per-model weekly windows, consume-first strategy | global credential swap moves every session at once |
| cwarm | Haiku warm-up per account on cron, skip-if-warm, missed-warmup <1h rule | no routing |
| ccas / ccs / relays | nothing | per-request proxy routing = the banned pattern |

Nobody does compact-before-switch. Anthropic closed native multi-account (#64376) as not planned.

## Facts this design rests on

- Keychain item is keyed per `CLAUDE_CONFIG_DIR` (default `Claude Code-credentials`, custom dir gets a suffixed item). Source: cc-pool.
- Refresh tokens are single-use and rotate with reuse detection: two owners of one token sign each other out.
- 5h window starts at the first message floored to the half hour (:00/:30) and resets 5h later (S1: warm at 09:31:32Z → resets_at 14:29:59.6Z; earlier windows reset 09:30Z). Weekly resets land on the hour.
- Statusline stdin carries `rate_limits.five_hour|seven_day.{used_percentage,resets_at}` (subscribers, after first response) and `prompt_cache`.
- Prompt cache is isolated per organization → every account switch is one uncached first turn.
- Main-conversation cache TTL is 1h on a subscription within plan usage; drops to 5m on usage credits.
- `/compact` while the cache is warm reads the prefix from cache (cheap); cold it re-reads everything. It is itself a model call, so it cannot run on an exhausted account.
- "Opus limit"/"Sonnet limit" are per-model; switching model family keeps working on the same account.
- `autoContinueAtUsageLimit` makes Claude Code wait for reset instead of exiting.

## Layout on disk

```
~/.cpool/
  config.toml               accounts, start hour, thresholds, strategy
  state.json                per-account usage + session table (flock'd)
  accounts/<name>/          CLAUDE_CONFIG_DIR for that account
    <entry> -> ~/.cpool/shared/<entry>   symlink per shared entry
    .claude.json            real, per account
    daemon/ ide/            real, per account (PID/lock files)
    .credentials*           never touched; Keychain item belongs to Claude Code
  handoff/<session>.json    switch requests from the mod to the wrapper
  log/                      jsonl: route, switch, warm, poll, cache-miss
```

### Central home and migration

All accounts end up under `~/.cpool/accounts/<name>/`; shared entries live once in `~/.cpool/shared/` and every account (including the former `~/.claude`) symlinks into it.
Two ways in:
- `cpool add <name>`: new folder, `claude /login` in it.
- `cpool adopt <dir> --name <name> [--move]`: register an existing dir (`~/.claude2`, `~/.claudeMax`). Without `--move` it is used in place (supported, not preferred). With `--move` the folder moves to `~/.cpool/accounts/<name>/` and the old path becomes a symlink to it, so existing aliases keep working.

Keychain fact (verified 2026-11-01): the item for a custom dir is `Claude Code-credentials-<sha256(CLAUDE_CONFIG_DIR string)[:8]>`, hashed over the exact string (`$HOME/.claude2` → `8c57e74e`; a trailing `/` or a different path is a different item). The default dir with no env var uses the bare `Claude Code-credentials`.
**Launch path ≠ storage path (decided 2026-11-01, replaces the re-login plan).** An account's `dir` in config is the *launch string* cpool sets as `CLAUDE_CONFIG_DIR`, so its Keychain item never changes; the files live wherever that path resolves. `--move` moves the folder to `~/.cpool/accounts/<name>/` and leaves the old path as a symlink to it; `dir` stays the old string (`~/.claude2`), so no re-login, no Keychain change, aliases keep working. The default account has `dir = "default"`: cpool launches it with `CLAUDE_CONFIG_DIR` unset (bare item), `~/.claude` becomes a symlink to `accounts/main/`, and `~/.claude.json` stays where it is (Claude Code reads it there for the default dir). No `adopt --finish`; cpool never deletes or creates a Keychain item. Moving refuses while any `claude` process is running (this session included).
`.claude.json` is per account, never shared.
v1 pool (decided 2026-11-01): `main` (`~/.claude`) and `claude2` (`~/.claude2`). `~/.claudeMax` stays out of the pool and untouched.

Shared via symlink (into `~/.cpool/shared/`): `projects/` (so `--resume` works across accounts), `settings.json`, `CLAUDE.md`, `skills/`, `agents/`, `plugins/`, `commands/`, `hooks/`, history. Per account: credentials, `daemon/`, `ide/`, MCP OAuth state (re-auth per account; known cost).

## Components

1. **CLI `cpool`** (Python 3.12+, stdlib + `tomllib`):
   `add <name>` (mkdir, mirror, run `claude /login` under the dir) · `remove` · `list` · `status` (table: 5h%, 7d%, resets, warm/cold, sessions) ·
   `run [--model M] [--account A] -- <claude args>` · `switch [<session>]` · `plan` (print the warm-up schedule) · `warm [--due]` · `poll` · `doctor`.
2. **Poller** (launchd agent, `cpool poll --daemon`): every 10 min per idle account, read-only GET `/api/oauth/usage` with the token read from that account's Keychain item. Expired token → skip, keep last-known. 429 → exponential backoff. Never refreshes. Accounts with a live session are skipped (statusline is fresher).
3. **Warmer** (launchd agent, `cpool warm --due` on calendar intervals; `pmset repeat wake` set by `cpool plan --apply`): `CLAUDE_CONFIG_DIR=<acct> claude -p --model haiku "ok"`; skip if account already in a window; a warm-up missed by >1h is skipped.
4. **Plugin `cpool`** (Claude Code plugin, enabled in shared settings so every account loads it):
   - statusline wrapper: writes the stdin JSON's `rate_limits`, `context_window`, `prompt_cache`, `session_id` into `state.json` keyed by account (from `CLAUDE_CONFIG_DIR`), then chains to the user's own statusline command.
   - handoff mod: at Stop, if the account is over the soft threshold, submit `/compact`; when compaction finishes, write `handoff/<session>.json` and end the session.
5. **Wrapper loop** (`cpool run`): pick account → exec `claude` as a child with `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_AUTO_CONTINUE…`/setting off → on child exit, if a handoff file exists for its session, pick the next account and relaunch `claude --resume <id>`; else exit with the child's code.

## Router

Eligible: usage credits off; `five_hour < soft` (85); `seven_day < weekly_soft` (90); requested model not model-limited; not in cooldown (5 min after being left).

Score among eligible (higher wins):
```
headroom  = 0.7*(100-u5h) + 0.25*(100-u7d)
expiring  = 25 * (100-u5h)/100 * max(0, 1 - mins_to_5h_reset/120)   # spend what resets soon; 0 with no open window
pace      = clamp(0.3 * (100*f - u7d), -15, 15), f = 1 - (7d_reset - now)/7d clamped to 0..1   # weekly pacing; 0 with no weekly reset
affinity  = +10 if another live session in the same cwd is on this account (shared cache)
load      = -3 per live session on the account
stale     = -20 if usage data older than 20 min
```
Capacity (S4: `main` is Max 5x, `claude2` is Pro): `headroom` is multiplied by `capacity / max_capacity_in_pool`, so 10% left on a 5x account outranks 40% left on Pro. Thresholds (`soft`, `weekly_soft`, `projected`) stay per-account percentages.
Sticky: a session never moves unless its account crosses `soft`. Move needs the target to beat the current by `margin` (10), for hysteresis. Nothing eligible → stay, and report the earliest reset.
Leave rule: the statusline only marks a session to leave (`leave`) when the router can name another account for it, chosen as for a forced handoff. If every other account is over its thresholds, `leave` stays unset and `leave_blocked` records why, so the session keeps running instead of exiting with nowhere to go. Manual switches skip this check.

## Staggered windows

Config: `start_hour = S` (default 8), accounts A1..AN. Offset step `k = max(1, round(5/N))` h.
Ai warm-up hour = `S - 5 + (i-1)*k` → first reset at `S + (i-1)*k`, then every 5h after its first real message.
N=2, S=8: A warm 3:xx (resets 8,13,18,23), B warm 5:xx (resets 10,15,20). N=3: 3:xx, 5:xx, 7:xx.
Re-anchor: at each scheduled boundary, warm an account only if it is idle (no window open); this keeps phase without spending real usage. Adding an account re-plans.

## Handoff

Trigger at Stop (turn boundary only): `u5h >= soft`, or `u5h + est_next_turn >= 97` where `est_next_turn` = the session's median per-turn % over its last 5 turns.
1. mod submits `/compact` on the current account (warm cache → cheap).
2. after compaction, mod writes `handoff/<session>.json {from, reason, session_id, model}` and exits the session.
3. wrapper routes (excluding `from`), relaunches `claude --resume <id> --model <same>` under the new dir. First turn reads only system prompt + summary uncached.
Hard-limit fallback (limit hit mid-turn, no compaction possible): wrapper sees the limit banner/exit, resumes on another account, mod compacts there first (one full cold read, which is the cost the soft threshold exists to avoid).
Model-limit only (Opus limit, account still has room): prefer moving to another account on the same model over a model switch; both bust the cache, but the user picked the model.

## Cache policy

Same model across a switch; default 1h TTL; never switch an uncompacted context over ~30% window; affinity keeps same-cwd parallel sessions on one account; log `prompt_cache` misses per switch so a costly switch is visible in `cpool status`.

## Safety

No proxy; no token writes; poll is read-only and slow; relaunches staggered with jitter (5–15s) so several sessions never hit a new account at once; warm-ups only Haiku "ok"; usage-credit accounts excluded; everything journalled.

## Spikes (before build)

- S1 Haiku `claude -p` starts the shared 5h window, and `resets_at` lands on the hour. Needs an account with no open window.
- S2 A plugin mod can submit `/compact`, observe it complete, and end the session cleanly; the wrapper sees the exit. (cadre's agents mod uses `$.prompt.submit`; confirm `/compact` and exit work the same.)
- S3 `claude --resume <id>` under account B resumes a transcript written under account A via the shared `projects/` symlink.
- S4 `/api/oauth/usage` request shape and the Keychain item name for a custom `CLAUDE_CONFIG_DIR`.
- S5 How usage-credits-on is detectable (usage endpoint field) so the router can exclude it.

## Phases

1. Spikes S1–S5, results appended here.
2. Accounts + mirror + `run` + router + `status` (one worker).
3. Telemetry: statusline wrapper + poller (one worker).
4. Staggering: `plan`/`warm` + launchd/pmset (one worker).
5. Handoff mod + wrapper loop (one worker; depends on 2, 3).
6. Cadre: document `command = "cpool run --"`; no code change in cadre.

## Spike results (2026-11-01)

- **S4** `GET https://api.anthropic.com/api/oauth/usage` (Bearer + `anthropic-beta: oauth-2025-04-20`) → 200 for both accounts, no rate-limit headers. Keys used: `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet` (`{utilization, resets_at ISO}`), `limits[]`, `extra_usage`, `spend`. Keychain JSON: `claudeAiOauth.{accessToken, refreshToken, expiresAt(ms), scopes, subscriptionType, rateLimitTier}`. main = `max` / `default_claude_max_5x`; claude2 = `pro` / `default_claude_ai`. Contract updated (capacity, epoch conversion).
- **S5** `extra_usage.is_enabled` (primary) and `spend.enabled`; both false on both accounts (`user_disabled: true`). Enabled case not observed.
- **S1** PASS (amended). claude2 window closed at 09:30Z; Haiku `claude -p "ok"` at 09:31:32Z opened it with `resets_at` 14:29:59.6Z and utilization 0.0 (a warm-up costs <1%). Rule: reset = floor30(first message) + 5h. Staggering keeps the formula but works in 30-min slots: warm-up fires at slot start + 2 min (clock skew), e.g. reset 08:00 ← warm 03:02. `resets_at` carries sub-second jitter: compare to the minute.
- **S3** PASS: session created on main, `projects/<slug>` symlinked into claude2, `--resume <id>` under claude2 recalled the codeword; same session id, transcript appended through the symlink.
- **S2** PASS. Protocol: in `turn.complete`, defer via `$.clock.after(500, …)` (compact rejects mid-turn), `await $.session.compact()` (resolves on completion with `{tokensBefore, tokensAfter}`, or `{skip}` if a hook vetoed), write the handoff file, `await $.command.run({command:'exit', args:''})`. Wrapper sees exit 0 (mod cannot set a code; the handoff file is the signal). `$.prompt.submit('/compact'|'/exit')` is rejected by the host. Haiku compaction ~4–7s. Full mod types: `~/.claude2/local-marketplaces/cadre-local/plugins/cs-agents/.claude-plugin/types/claude-code/index.d.ts`. Live runs need a trusted cwd (trust prompt blocks). Spike mod: scratchpad `spikeS2/mod`.
