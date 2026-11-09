# cpool

cpool lets you use several Claude subscriptions from Claude Code as one pool. Each session starts on the account with the most room left. When an account gets close to its limit, cpool compacts the session and resumes it on another account. It also spreads the 5-hour windows across the day so a reset is usually not far away.

It runs the official `claude` CLI. There is no proxy, and cpool never copies, refreshes or exports a login.

macOS only for now.

## How it works

Each account is its own Claude Code config dir (`CLAUDE_CONFIG_DIR`). Claude Code keeps each dir's login in its own Keychain item, so the accounts stay separate. Things you want everywhere (`projects/`, settings, `CLAUDE.md`, skills, agents, plugins, history) live once in `~/.cpool/shared/` and every account dir links to them. Because `projects/` is shared, a session started on one account can be resumed on another.

cpool keeps usage per account in `~/.cpool/state.json` from two sources:

- The statusline. While a session runs under `cpool run`, Claude Code hands cpool's statusline the account's 5-hour and weekly usage after every response.
- A poller. Every 10 minutes it reads usage for idle accounts from the same endpoint Claude Code's `/usage` uses. It only reads.

### Picking an account

`cpool run` scores every account and picks the best one. An account is skipped when:

- its 5-hour usage is at 85% or more
- its weekly usage is at 90% or more
- the weekly limit for the model you asked for is reached
- usage credits are turned on for it

Headroom is weighted by plan size, so 30% left on a Max 5x account beats 60% left on a Pro account. An unused 5-hour window that is about to reset gets a bonus of up to 25 points, fading to nothing when the reset is two hours away, so usage gets spent before it disappears. The weekly window adds a pacing term of up to 15 points either way: an account behind its weekly pace is favoured, one ahead of it is held back. Inside 36 hours of its weekly reset, an account gets up to 100 points scaled by its unused weekly allowance, so allowance that would otherwise be lost gets used first. A session stays where it is unless another account is clearly better.

### Moving a session

At the end of each turn the statusline checks whether the session should leave its account: 5-hour usage at the threshold, the next turn likely to cross 97%, or the model's weekly limit reached. If so, a small Claude Code plugin compacts the session on the current account (its cache is still warm, so this is cheap), writes a handoff file and exits. `cpool run` sees the file, picks the next account and runs `claude --resume` with the same session and model. The new account starts with the compacted summary, so the first turn there reads very little uncached context.

A session only leaves when another account can take it. If every other account is over its limits, the session stays put and the statusline records why in `leave_blocked`.

`cpool switch` asks the current session to move at the end of its next turn.

### Staggered windows

A 5-hour window starts at your first message, rounded down to the half hour. cpool can start each account's window early with a one-word Haiku call so the resets land at times you choose. With `start_hour = 8` and two accounts:

| account | warm-up | resets |
|---|---|---|
| main | 03:02 | 08:00, 13:00, 18:00, 23:00 |
| claude2 | 05:32 | 10:30, 15:30, 20:30 |

A warm-up only runs when the account has no window open, so it never cuts a window short. The schedule runs from launchd. To have the Mac wake for the early ones, `cpool plan --apply` prints a `pmset` command for you to run with sudo.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/ayorcodes/cpool/main/install.sh | sh
```

No Python or other dependencies needed: it installs a prebuilt binary to `~/.local/share/cpool/cpool.dist` and links `~/.local/bin/cpool` to it. Re-run to upgrade. Uninstall with `curl -fsSL https://raw.githubusercontent.com/ayorcodes/cpool/main/install.sh | sh -s -- --uninstall`.

From source:

```sh
git clone https://github.com/ayorcodes/cpool
cd cpool
uv tool install .
```

Building the release binary (maintainers; needs Python 3.12+ and Xcode CLT):

```sh
scripts/build-binary.sh   # writes release/cpool-darwin-<arch>.tar.gz and .sha256
gh release create vX.Y.Z release/*
```

## Setup

Close every running `claude` first. cpool refuses to move folders while one is open.

Register the accounts you already have:

```sh
cpool adopt ~/.claude --name main
cpool adopt ~/.claude2 --name claude2 --move
```

`--move` puts the folder in `~/.cpool/accounts/<name>` and leaves a symlink at the old path, so existing aliases keep working. Your logins stay where they are because cpool launches each account with the same path it had before.

When two accounts both have a file cpool wants to share, nothing is deleted. Transcripts are merged, history is appended, and any clash is kept next to the original as a `.cpool-conflict-<account>` or `.cpool-bak-<account>` copy.

Add a new account:

```sh
cpool add work
```

Check everything:

```sh
cpool doctor
cpool status
```

Turn on the warm-up schedule and the poller:

```sh
cpool plan           # show the schedule
cpool plan --apply   # install the launchd jobs
```

## Use

```sh
cpool                             # instead of claude
cpool --model opus                # claude flags go straight through
cpool --resume <id>               # resume a session on the best account
cpool run "prompt"                # a positional prompt needs run, since a bare first word is read as a subcommand
cpool run --account claude2 --    # pin an account
cpool status                      # usage per account and the next pick
cpool switch                      # move the current session at the end of its turn
```

In print mode (`-p`) cpool never relaunches. The run finishes, and the next run goes to another account.

### With cadre

Set the role command to `cpool run --` and every role is routed through the pool. cadre needs no other change.

## Config

`~/.cpool/config.toml`:

```toml
[pool]
start_hour = 8     # first reset of the first account
soft = 85          # leave an account at this 5-hour %
projected = 97     # or when the next turn would likely cross this
weekly_soft = 90   # skip an account at this weekly %
# expiry_h = 36        # hours before a weekly reset at which unused weekly allowance earns the bonus
# expiry_weight = 100  # points for 100% unused weekly allowance inside that window
margin = 10        # how much better another account must be to move
cooldown_min = 5   # an account just left is skipped this long
poll_min = 10

[[account]]
name = "main"
dir = "default"    # the plain ~/.claude login
order = 1

[[account]]
name = "claude2"
dir = "~/.claude2"
order = 2
```

`order` sets the warm-up order. `capacity` can override the plan size cpool reads from the login.

## Advisor

`cpool advise` sends a question plus a facts file to Fable on an Anthropic API key, so it spends the API credit a Max plan includes instead of the subscription. Fable never runs tools or reads the disk; it sees only the question, the facts and any `--attach` files.

Setup: `cpool advise setup` stores the key in the Keychain (service `cpool-advisor`), checks it reaches the model, and installs the `ask-fable` and `ask-opus` skills (into `~/.cpool/shared/skills` when it exists, else `~/.claude/skills`). It also adds `Bash(cpool advise:*)` to `permissions.allow` in `~/.cpool/shared/settings.json` when that file exists, else `~/.claude/settings.json`, and leaves the file alone if it isn't valid JSON. Nothing else to do: the skills trigger on "ask Fable" / `/ask-fable` and "ask Opus" / `/ask-opus` (the latter pins `--model claude-opus-5-5`). `cpool advise setup --remove` undoes the skills and the allow rule (add `--force` to delete a modified skill); the Keychain key stays until `key clear`.

Use:

```
cpool advise ask "Which design?" --facts facts.md [--attach FILE ...] [--thread new|ID] [--json]
cpool advise key set | clear | status [--check]
cpool advise usage [--json]
```

A thread allows `max_rounds` rounds; the last one asks Fable for a verdict. Exit codes: 0 verdict, 3 Fable needs more facts (answer them with `--thread ID`), 2 usage/config/key/API error, 4 cap reached, 5 refusal.

Cap: every call first reserves its worst-case cost (input tokens at the 1h cache-write price plus `max_tokens` of output) and is refused when this cycle's spend plus that reservation would pass `monthly_cap_usd`. The cycle resets on `cycle_day`. Spend is kept in `~/.cpool/advisor/ledger.jsonl`.

```toml
[advisor]
model = "claude-fable-5-2"
effort = "high"            # low|medium|high|xhigh|max
max_tokens = 32000
monthly_cap_usd = 100.0
cycle_day = 1              # 1..28
max_rounds = 3             # 1..6
max_attach_bytes = 2000000
```

## Things to know

- Running more than one account is your call. Anthropic's consumer terms forbid sharing a login and automated access outside the official tools. cpool only drives the official CLI with your own logins and never uses usage credits, but read the terms yourself.
- MCP servers that need OAuth have to be authorized once per account.
- Prompt caches don't carry across accounts, so the first turn after a move is uncached. Compacting first keeps that turn small.
- If a handoff fails in a headless session that has gone idle, it waits until the session gets another turn.
- The handoff has been tested with a fake `claude`. A live run across two real accounts is still to do.

## Development

```sh
uv run --extra dev pytest -q
claude plugin test plugin
```

Design notes are in `docs/design/`.
