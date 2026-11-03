# cpool advise: a Fable advisor paid from API credits

Status: approved 2026-11-03. Size: medium. Stakes: critical (money cap, API secret).

## Why

Max includes $100/month of Claude API credit. The credit expires at the end of each billing cycle, Claude Code can't spend it, and Pro includes none. The built-in Claude Code advisor (`advisorModel`) runs on Anthropic's servers inside the session's own request, bills to the session's own credential, and has no hook. Setting `ANTHROPIC_API_KEY` would move the whole session onto API billing.

So cpool adds a separate advisor. The session gathers facts with its usual subscription agents, `cpool advise` sends the question and facts to Fable on the API key, and Fable answers with either a verdict or a list of facts it still needs. Fable never runs tools and never reads the disk; cpool inlines only the files the session names. The built-in advisor stays as it is (Opus, subscription) for routine gates.

## Scope

In:
- `cpool advise` (ask / key / usage / setup)
- the `[advisor]` config section
- the Keychain key
- the spend ledger and cap
- threads
- the `fable` skill, shipped in the package and installed by `setup`
- tests and the README

Out:
- cadre wiring
- the GUI
- an MCP server
- Pro/Opus-specific behaviour (`model` in config already covers it)
- any change to `cpool run` or the plugin

## Files

| file | change |
|---|---|
| `src/cpool/advisor.py` | new. All logic: keychain, ask, ledger, threads, cap, skill install |
| `src/cpool/assets/fable/SKILL.md` | new. Skill text, verbatim from §Skill |
| `src/cpool/config.py` | `Advisor` frozen dataclass and `_parse_advisor`. `Config` gains `advisor: Advisor = field(default_factory=Advisor)` as its LAST field, so `Config(Pool(), ())` (config.py:129) still works |
| `src/cpool/paths.py` | `advisor_dir() -> home() / "advisor"` |
| `src/cpool/cli.py` | `advise` subparser with nested subparsers (the first nested one in cli.py). Dispatch in the `match` at cli.py:289. Map `advisor.AdvisorError` into the exit-2 except at ~cli.py:326 |
| `pyproject.toml` | `dependencies = ["anthropic>=<current 1.x>"]`. Use the official SDK, not urllib: this is cpool's first runtime dependency, accepted |
| `tests/test_advisor.py` | new, see §Tests |
| `README.md` | an "Advisor" section: setup, use, exit codes, cap |

## Config: `[advisor]` in `~/.cpool/config.toml`, every key optional

```toml
[advisor]
model = "claude-fable-5-1"      # must be a key of advisor.PRICES
effort = "high"                 # low|medium|high|xhigh|max
max_tokens = 32000
monthly_cap_usd = 100.0         # > 0
cycle_day = 1                   # 1..28, the local day the credit cycle resets
max_rounds = 3                  # 1..6
max_attach_bytes = 2000000      # total for facts plus attachments, per call
```

Unknown keys raise `ConfigError` (same pattern as `_parse_pool`, config.py:58-62). Wrong types or out-of-range values raise `ConfigError`.

## Prices

The hardcoded `advisor.PRICES` is in USD per million tokens and is checked against the live pricing page. Each entry is `(input, output, cache_write_1h, cache_read)`:
- `claude-fable-5-1`: (10, 50, 20, 0.25)
- `claude-opus-5-5`: (4, 20, 8, 0.20)

A one-hour cache write costs 2× input. Cost is computed from `usage` as:

input × in + output × out + cache_creation × cache_write_1h + cache_read × cache_read

When `response.model` isn't in `PRICES` (a fallback rescue), charge at the highest-priced entry. Overestimating is safe for a cap.

## Key

- Stored in the Keychain: service `cpool-advisor`, account `getpass.getuser()`.
- **Read:** `security find-generic-password -s cpool-advisor -a <user> -w`.
- **Write:** prompt with `getpass`. Validate against `^sk-ant-[A-Za-z0-9_-]{20,}$`. Write through `security -i` with the command on **stdin** (`add-generic-password -U -s cpool-advisor -a <user> -w <key>\n`), so the key never appears in argv or `ps`.
- **Clear:** `delete-generic-password -s cpool-advisor -a <user>`.
- Every `security` call goes through one private `_security(args, stdin=None)` wrapper; tests stub that.

Hard rules:
- The key never goes into `os.environ`, argv, a file, the ledger, the journal or the output.
- The client is built with `api_key=<key>` and `base_url="https://api.anthropic.com"`, both explicit. Check how the installed SDK resolves `auth_token` / `ANTHROPIC_AUTH_TOKEN` from the env, and make sure no env credential or base URL is sent; a test asserts this.
- `journal.log` gets only counts, cost, model, thread and round.

## CLI

```
cpool advise ask [QUESTION] --facts FILE [--attach PATH ...] [--thread new|ID] [--model M] [--effort E] [--json]
cpool advise key set | clear | status [--check]
cpool advise usage [--json]
cpool advise setup [--force]
```

### ask

- QUESTION comes from the positional argument, else stdin.
- `--thread` defaults to `new`.
- **User content:**
  - `<question>…</question>`
  - `<facts>` + the file's text + `</facts>`
  - one `<file path="ABS">…</file>` per `--attach`
  - Over `max_attach_bytes`, refuse (exit 2) and name the sizes. Never truncate silently.
- **Thread:** stored at `advisor_dir()/threads/<id>.json` as `{id, model, created, messages:[...]}`.
  - A new id is `YYYYMMDD-HHMMSS-<4 hex>`.
  - The model is fixed per thread; `--model` on an existing thread that differs is an error.
  - Messages are append-only. The assistant turn is stored as the full response content blocks, unchanged, thinking blocks included (`block.model_dump(exclude_none=True)` or the SDK's dict form).
- **Rounds:** round = the number of user messages after appending.
  - round > `max_rounds` → exit 2.
  - round == `max_rounds` → append to the user content: `This is the final round. Give a VERDICT with the facts you have and state your assumptions.`
- **Cap and reservation:** take an `fcntl.flock` on `advisor_dir()/ledger.lock`.
  - `in_tok` = `client.messages.count_tokens(model, system, messages)`.
  - `worst` = in_tok × cache_write_1h + max_tokens × out.
  - `spent` = the cycle's finals + reservations younger than 2h with no final.
  - If spent + worst > cap: exit 4 with `spent $X of $CAP this cycle; this call could cost up to $W`.
  - Otherwise append the reservation row `{ts, kind:"reserve", id, thread, round, worst}` and release the lock. The lock is never held during the call.
- **The call:**
  - Request: `client.beta.messages.stream(model, max_tokens, system=[{type:text, text:SYSTEM}], messages, output_config={"effort": effort}, cache_control={"type":"ephemeral","ttl":"1h"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")`, then `.get_final_message()`.
  - No `thinking` param: Fable is always adaptive, and `disabled` / `budget_tokens` return a 400.
  - Confirm each parameter against the installed SDK. The beta top-level `cache_control` with `ttl` and `fallbacks` must exist; if one doesn't, stop and report it rather than improvise.
- **Recording:** append the final row `{ts, kind:"final", id, thread, round, model, stop_reason, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, cost_usd}`, same `id` as its reservation.
  - On an API error: append `{kind:"final", id, cost_usd:0, error:<class name>}` and exit 2 with the error message.
  - `refusal` → also record, print `stop_details`, exit 5. Don't save the turn to the thread.
- **Parsing the answer:** text = the concatenated `text` blocks.
  - A line starting `NEEDS:` → status `needs`; items are the following `-`/`*`/numbered lines.
  - Else a line starting `VERDICT:` → `verdict`.
  - Else `unstructured`, treated as a verdict.
  - `stop_reason == "max_tokens"` → status as parsed, plus `truncated: true` and a stderr warning.
- **Output:**
  - Plain: the text, then a footer `[advise] thread=<id> round=<n>/<max> model=<m> cost=$<c> cycle=$<spent>/$<cap>`.
  - `--json`: `{status, text, needs[], thread, round, max_rounds, model, cost_usd, cycle_spent_usd, cap_usd, truncated, usage{...}}`.
- **Exit codes:**

  | code | meaning |
  |---|---|
  | 0 | verdict or unstructured |
  | 3 | needs |
  | 2 | usage, config, key or API error |
  | 4 | cap |
  | 5 | refusal |

### Cycle

- Cycle start = the latest local date ≤ today whose day is `cycle_day`, at 00:00 local. Before the month's `cycle_day`, that's the previous month.
- The ledger is `advisor_dir()/ledger.jsonl`, append-only.
- `spent` sums `final.cost_usd` with ts ≥ start, plus open reservations as above.

### key status / usage / setup

- **`key status`:** reports present or absent (never the value). `--check` runs `client.models.retrieve(model)` (free) and reports whether the key reaches the configured model.
- **`usage`:** cycle start, spent, cap, remaining, calls this cycle, open reservations.
- **`setup`:**
  1. `key set` if absent.
  2. `key status --check`.
  3. Install the skill: copy the packaged `assets/fable/SKILL.md` to `paths.shared_dir()/skills/fable/SKILL.md` when `paths.shared_dir()/skills` exists (cpool shares skills across accounts, README:11), else to `~/.claude/skills/fable/SKILL.md`. If a different file is there, refuse unless `--force`.
  4. Add `Bash(cpool advise:*)` to `permissions.allow` in the user settings file. Amended 2026-11-03: setup must be hands-off.
     - Target: `paths.shared_dir()/settings.json` when it exists (shared across accounts), else `~/.claude/settings.json`. Create the file if it's missing.
     - Idempotent: if the rule is already present, change nothing.
     - Preserve every other key and its order. Write atomically (temp file + `os.replace`), with `indent=2` and a trailing newline.
     - If the file isn't valid JSON, or `permissions`/`allow` has the wrong type, refuse this step with a message and leave the file untouched.
  5. Print a one-line summary per step (`done` / `already` / `skipped: <why>`).

  No CLAUDE.md edit: the skill's own description triggers on "ask Fable".

  `cpool advise setup --remove` undoes steps 3–4: it deletes the skill dir only if its SKILL.md matches the packaged one (else skips, unless `--force`), and removes only the exact allow rule. The Keychain key stays; `key clear` removes it.

Read packaged assets with `importlib.resources`, and make sure hatchling ships `assets/` in the wheel.

## System prompt (stable; the cached prefix)

```
You are the senior advisor to an engineer's coding session. You cannot run tools, read files, or see the repository: everything you know is in the question, the facts, and any attached files. The session will act on your answer and can gather more facts for you with cheap read-only searches.

Answer in exactly one of two forms.

VERDICT: the decision or plan, first, in a sentence. Then the reasoning that decides it, the risks and how to contain them, and what the session should verify before or after acting. Be concrete: name files, functions, steps.

NEEDS: only when a missing fact would change the decision. List at most 7 items, each a bounded question a read-only search can answer (name the files, symbols or commands to look at) and one line on why it matters. Never ask for something the facts already give.

Prefer VERDICT whenever the facts support a sound decision; state the assumptions you made instead of asking. Be direct and dense; no preamble.
```

## Skill (`assets/fable/SKILL.md`, verbatim)

```markdown
---
name: fable
description: Ask Fable for a decision, plan or diagnosis on a hard problem, paid from API credits instead of the subscription. Use when the user says "ask Fable", "hand it to Fable", "get Fable's verdict", or runs /fable.
---

Fable advises; this session does the work. Fable cannot read the repo or run anything, so its answer is only as good as the facts you send.

1. State one decision question. If the request holds several, ask them one at a time.
2. Gather the facts with the cheapest means you have (scouts or subagents, Grep, Read). Write them to a file in your scratchpad: the goal, constraints, what has been tried and how it failed, the options you see, and the code that matters as `file:line` plus the verbatim excerpt. Attach a whole file with `--attach` only when all of it matters. Never include secrets.
3. Run: `cpool advise ask --thread new --facts <file> [--attach <path> ...] --json "<question>"`
4. Exit 3 (`status: needs`): answer each item in `needs` the same cheap way, write the answers to a new facts file, then run `cpool advise ask --thread <thread> --facts <new file> --json "Answers to your NEEDS."`. Repeat until you get a verdict.
5. Exit 0: give the user the verdict in a few lines, then carry it out here as normal work. Exit 4: the monthly credit cap is reached; tell the user and stop asking. Exit 5: Fable refused; report it. Exit 2: report the error.

Never set or export ANTHROPIC_API_KEY. `cpool advise usage` shows this cycle's spend.
```

## Tests (`tests/test_advisor.py`)

Each test has a stated purpose. Isolate with `monkeypatch.setenv("CPOOL_HOME", str(tmp_path))` (pattern from test_usage.py:32). Inject a fake client through a module-level factory (e.g. `advisor._client = lambda key: Fake()`) and stub `advisor._security`. No network.

1. **Cap (money):**
   - With a ledger near the cap, `ask` exits 4, makes no messages call, and writes no reserve row.
   - Under the cap, a reserve row is written and then a final row with the computed cost.
   - An open reservation counts toward spent.
2. **Cycle boundary (money):** rows before the cycle start don't count; `cycle_day` falls back to the previous month.
3. **Thread round-trip (protocol):**
   - Round 2 sends round 1's user message plus the assistant content blocks unchanged.
   - Round `max_rounds` carries the final-round line.
   - Round `max_rounds + 1` exits 2.
4. **Secret hygiene (security):** after an `ask`, neither the ledger, the thread file, stdout nor the journal contain the key. The fake client got `api_key=<key>` and an explicit base_url.
5. **Config:** an unknown `[advisor]` key raises `ConfigError`, and a missing `[advisor]` gives the defaults. Extend an existing config test if there is one.

## Done when

- `uv run --extra dev pytest -q` passes.
- `cpool advise --help`, `cpool advise usage` and `cpool advise key status` run against a temp `CPOOL_HOME` with no key, without a traceback.
- `uv build`, and the wheel contains `cpool/assets/fable/SKILL.md`.

A live call is the user's step after `cpool advise setup`.
