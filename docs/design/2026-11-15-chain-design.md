# cpool chain: handoffs that keep subagents, shard that keeps the stagger

Status: proposal, 2026-11-15. Four parts, built in this order. Part 1 is the
one that hurts today.

## Problem

1. A handoff while background subagents run goes wrong in three places:
   - `plugin/hooks/register.ts:83` leaves at the first main-loop
     `turn.complete` after `leave` is set, whether or not subagents are still
     in flight.
   - `$.command.run({command: 'exit'})` (`register.ts:72`) then meets Claude
     Code's "background tasks are running" confirm, so the user is asked and
     usually quits by hand.
   - The resumed session gets only the compact summary. It does not know which
     agents died or where their briefs and reports are, so it neither collects
     nor respawns them.
2. `run.py:270` `_hard_handoff` asks `[Y/n]` on a hard limit: another prompt in
   an unattended run.
3. In shard mode (`run.py:369`) there is no handoff at all. cliproxyapi
   round-robins requests (`/opt/homebrew/etc/cliproxyapi.conf:137`), so:
   - every account's 5-hour window opens at once, and the stagger is lost;
   - each subagent session lands on a different account and cold-writes the
     cache.
4. The 5-hour windows are staggered (`schedule.plan`), but nothing shows them
   as one pool, and the router only nudges toward the soonest reset
   (`router.py:115`, `reset_weight`).

## Part 1: subagent-aware handoff

### Track
- An `agent.spawn` hook in the mod records the spawn after `next(e)` resolves
  with `agentId`. The record holds `agentId`, `description`, `subagentType`,
  `model`, `background`, `prompt`, and `report`, which is the `Report:` line
  parsed from the prompt (the cadre-lite briefs carry it).
- Records live in `<home>/agents/<CPOOL_RUN>.json`, one map keyed by
  `agentId`.
- `turn.complete` with an `agentId` that is in the map marks it `done`.
  `$.agent.list()` is the live truth when we decide; the file is what
  survives the exit.

### Defer
On a main-loop `turn.complete` with `leave` set, read `$.agent.list()`. If any
agent with no `parentId` is `pending`, `running` or `waiting`:
- With `leave` = `soft` or `manual`, defer. Set nothing. The next main
  `turn.complete` (each subagent's completion notification produces one)
  re-checks. The subagents keep running on the current account. The 15% left
  below `soft` (85%) is usually enough for them to finish.
- With `leave` = `projected` or `model`, or with 5-hour usage at or above the
  new `pool.hard` (default 97), go to forced leave.
- The statusline shows `leave deferred: N agents`.

### Forced leave
- Write the handoff file as today, plus `agents: [records not done]`.
- Don't call `exit`. `run.py` already polls for exit. It now also watches
  `<home>/handoff/<run>.json`. Once the file has been there 3 seconds, it
  sends the child SIGTERM, and SIGKILL after 10 seconds. That avoids the
  confirm prompt.
- Spike first: check whether `exit` with background tasks prompts, and
  whether SIGTERM leaves the transcript consistent. If `$.command.run('exit')`
  turns out to be silent, keep it.

### Resume
- `run.py` relaunches `--resume <sid>` with
  `CPOOL_RESUMED=<path to the taken handoff json>`. It keeps the file under
  `<home>/handoff/done/` instead of deleting it.
- The mod, on `session.start` when `CPOOL_RESUMED` is set, calls
  `$.prompt.submit` once. Interactive mode needs a submitted prompt to carry
  on, and print mode is never relaunched. The prompt says:
  > cpool moved this session from A to B (reason). These subagents were
  > stopped before they finished: for each, its description, type, model,
  > report path and brief. For each one, read its report if it exists and is
  > complete; otherwise spawn it again with the same brief, type and model.
  > Then continue the task where the summary left off.
- This drops the "never submits a prompt" rule in `register.ts:6`. Update
  `docs/design/2026-11-01-cpool-contract.md` to match.
- If the reason was a hard limit mid-turn, the prompt also says to redo the
  last turn.

### Hard limit with no handoff file
`_hard_handoff` stops asking. A relaunch happens when
`pool.auto_hard = true` (the default) or stdin is not a TTY, and the resume
prompt above covers it. With `auto_hard = false`, it keeps today's `[Y/n]`.

### Tests
- `plugin/hooks/handoff.test.ts`:
  - leave is deferred while an agent runs;
  - forced leave writes `agents`;
  - the resume submit fires once.
- `tests/`:
  - the wrapper SIGTERMs after the handoff file appears;
  - `CPOOL_RESUMED` is passed;
  - `_hard_handoff` does not prompt when `auto_hard` is on.

## Part 2: shard fill-first and priority sync

Shard keeps its per-request routing, but fills one account at a time in chain
order. Subagents then share the main session's account and its cache, and
only one 5-hour window opens at a time.

- `cpool doctor` fails a shard setup unless cliproxyapi has
  `routing.strategy: fill-first` and `session-affinity: true`. cpool never
  writes that config itself.
- The poller (every `poll_min`) and the statusline (once a minute at most)
  compute the chain order:
  - eligible accounts sorted by 5-hour reset, soonest first, then by score;
  - ineligible accounts last.
- They then PATCH `/v0/management/auth-files/fields` with
  `priority = 100 - rank` per auth file. Auth files map to accounts by email
  through `shardacct.account_emails`.
- They skip the PATCH when the order hasn't changed and journal
  `shard-order`.
- Unverified (spike): whether fill-first picks the highest priority first or
  the file order; how `quota-exceeded` cooldown ends at a 5-hour reset; the
  management key and auth header.

## Part 3: chain view

- The statusline and `cpool status` get one pool line:
  ```
  pool ██████████▓▓▓░░░ 2.1/3 · runway 4h10 · next +1 claude2 10:30 · seamless
  ```
- Pooled room is Σ (100 − u5h)/100 × capacity / max capacity, counted in
  whole windows.
- Burn is the median Δu5h per hour of active time over the session's turns
  (the same data as `should_leave`), scaled by capacity.
- Runway is pooled room ÷ burn.
- The line says `seamless` when runway covers the time to the next reset,
  else `gap ~Xm at HH:MM`.
- A weekly figure sits on the same line: pooled weekly percent and
  ahead/behind pace.

## Part 4: chain warm-ups and router strategy

- `pool.strategy = "chain"` makes `score` rank the soonest 5-hour reset first
  among eligible accounts. The current score only breaks ties.
- `pool.warm = "chain"` re-warms each idle account at its own reset, all day,
  and corrects its phase from the live `resets_at` rather than the 4 fixed
  slots in `schedule.plan:54`, which cover only 20h a day. Resets then drift
  against the clock day to day.
- The default stays `"clock"`.
- Fix the README: it says the reset bonus fades out 2h before reset, but
  `router.py:115` fades it over 5h.

## Out of scope
- Compacting subagents or carrying their context across accounts. A stopped
  agent is respawned from its brief.
- Usage credits and the API-key path.

## Checks run 2026-11-15 (Claude Code 2.1.325, probe plugin, Haiku)

1. **Running `exit` with a background agent in flight shows the "Background
   work is running" dialog**: Exit and stop / Move to background / Stay. So
   forced leave must not use `exit`.
   - SIGTERM to the `claude` process exits cleanly (rc 143) and prints
     "Resume this session with: claude --resume <sid>".
   - Forced leave is therefore: write the handoff file, then the wrapper sends
     SIGTERM.
2. **A main-loop `turn.complete` fires when a background agent finishes.**
   Its `$.agent.list()` row moves from `running` to `waiting` to `completed`.
   Deferring until the next main turn works.
3. **The `agent.spawn` hook has what we need.**
   - `next(e)` resolves with `{model, agentId}`; the input carries
     `description`, `subagentType`, `model`, `background` and `prompt`.
   - The hook fires again with the same `agentId` when a task notification or
     SendMessage resumes the agent. Its `prompt` is then the notification or
     message, so keep the first prompt recorded per `agentId`.
4. **`$.prompt.submit` from `session.start`, after a 1.5 s delay, works on a
   `--resume`d interactive session.** The model saw the earlier spawn in the
   transcript and answered the injected prompt.
5. **cliproxyapi** (source, `~/Work/CliProxyApi`):
   - Priority:
     - top-level `priority` is copied into attributes (`synthesizer/file.go:203`);
     - only the highest priority tier is eligible (`selector.go:313-341`);
     - equal priority goes to the lowest Auth.ID (`:338`);
     - fill-first takes `available[0]` (`:573`).

     Priority sync works.
   - A Claude 429 with a unified rejection is credential-wide. Its recovery
     time is parsed from `Anthropic-Ratelimit-Unified-5h-Reset` / `-7d-Reset`
     / `Retry-After` (`helps/claude_ratelimit.go:50-128`), so the account
     comes back at its real reset.
   - PATCH `/v0/management/auth-files/fields` with `{name, priority}` applies
     live and is persisted (`auth_files_fields.go:204,345`). The key goes in
     `Authorization: Bearer`.
   - The config holds only a bcrypt hash of the key, so the user must give
     cpool the plaintext key (Keychain). `cpool shard key set`.
   - Session affinity rebinds when the bound credential is unavailable or a
     request fails (`selector.go:761,856`). It never moves back, so sessions
     drift away from the chain order until the TTL drops the binding.
     Acceptable under fill-first.
   - The current auth files have no `priority`. The two Claude files are
     adeayo32 and adeayo35.
6. **The cache does not carry across accounts.** On one account, a fresh
   Haiku subagent still read its 35-49k shared prefix (system prompt and
   tools) from cache and wrote only about 16k. Under round-robin shard, each
   agent landing on another account writes that prefix in full. That is the
   measured source of the extra shard burn.
