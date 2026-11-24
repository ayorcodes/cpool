# ADR: Proxy-Free VS Code Claude Code Extension Support (`cpool-ide-exec`)

- **Status:** Accepted
- **Date:** 2026-11-23
- **Scope:** `cpool` packaging (`pyproject.toml`), new `cpool.ide_exec` entrypoint, optional `register.ts` IDE guard

---

## Context

`cpool` currently pools Claude Code accounts by spawning the `claude` CLI with a chosen account's `CLAUDE_CONFIG_DIR` (`accounts.py:25`), injecting `cpool statusline` + `--plugin-dir` (`run.py:331-389`), and performing mid-session handoffs by exiting and relaunching `claude --resume <session_id>` (`run.py:392-440`).

The official **VS Code Claude Code extension** (webview sidebar mode) spawns its own bundled `claude` binary and holds a persistent JSON-over-stdio connection to that child process. Users want `cpool` account pooling in VS Code **without running any local HTTP proxy** (ruling out `ANTHROPIC_BASE_URL` / shard mode / `cliproxyapi`).

Key existing `cpool` properties that shape the design:
1. **Shared transcripts already exist:** `mirror.py:14-20` symlinks `projects/`, `history.jsonl`, `settings.json`, `hooks/`, `plugins/`, `skills/`, `agents/`, `file-history/`, `todos/`, and `plans/` into `~/.cpool/shared`, leaving only auth and per-process state (`.credentials.json`, `.claude.json`, `daemon`, `ide`, `statsig`, `shell-snapshots`) per account. Any account can already `--resume <session_id>` any session.
2. **VS Code provides a process-wrapper hook:** `claudeCode.claudeProcessWrapper` accepts a single executable path. When set, VS Code invokes `<wrapper> <bundled-claude-bin> [args...]` instead of `<bundled-claude-bin> [args...]`.
3. **No `statusLine` in VS Code:** The extension webview does not run Claude Code's terminal `statusLine`, so `cpool statusline` will not feed live usage updates during IDE sessions. `cpool` already has OAuth usage polling (`usage.py:182` `poll_due`, `usage.py:223` `poll`, `usage.py:275` `poll_daemon`).

---

## Decision

Bundle a dedicated, proxy-free executable **`cpool-ide-exec`** in the `cpool` Python package that integrates directly with VS Code's `claudeCode.claudeProcessWrapper`.

### 1. Packaging & Entrypoint

Add a second console script in `cpool/pyproject.toml`:

```toml
[project.scripts]
cpool = "cpool.cli:main"
cpool-ide-exec = "cpool.ide_exec:main"
```

Exposing a standalone binary (`cpool-ide-exec`) rather than requiring `cpool ide-exec` avoids shell-quoting issues in `claudeCode.claudeProcessWrapper`, which expects a single executable path string:

```json
{
  "claudeCode.claudeProcessWrapper": "/absolute/path/to/cpool-ide-exec"
}
```

### 2. Wrapper Execution Contract (`cpool/src/cpool/ide_exec.py`)

When VS Code invokes `cpool-ide-exec <claude_bin> [args...]`:

1. **Validate argv:** If `len(sys.argv) < 2`, print usage to stderr and exit non-zero.
2. **Pass through non-session helper invocations untouched:** VS Code invokes the binary for lightweight checks (e.g., `--version`, `-v`, `--help`, `plugin`, `mcp`, `doctor`, `auth`, `update`, `setup-token`). Detect these subcommands/flags and immediately `os.execvp(claude_bin, sys.argv[1:])` without routing, polling, or modifying flags.
3. **Refresh stale usage (bounded):** Because `statusLine` does not run in VS Code, ensure routing state isn't stale by relying on `cpool`'s background poller (`poll_daemon`) and/or a fast, time-bounded `usage.poll_due(...)` call with a strict timeout (e.g., ≤ 1.5s) so chat startup never hangs if offline.
4. **Select account:** Parse `--model` from `argv[2:]` if present, then reuse `cpool`'s existing selection logic (`router.choose` + subscription/fallback checks, factored out of `run.py:195-223` into a shared helper). Log the routing decision to `journal.log("route", ...)` with `ide=True`.
5. **Prepare environment:**
   - Apply `accounts.launch_env(acct)` (sets or unsets `CLAUDE_CONFIG_DIR`).
   - Set `CPOOL_ACCOUNT=<acct_name>` and `CPOOL_IDE=1`.
   - **Do not** set `CPOOL_RUN` in Phase 1 (so `plugin/hooks/register.ts` will not attempt to write handoff files or trigger exit-and-relaunch, which would drop VS Code's stdio pipe).
   - **Do not** set `ANTHROPIC_BASE_URL` or proxy env vars.
6. **Direct `exec` handoff:** Call `os.execvpe(claude_bin, sys.argv[1:], env)`. The wrapper process is replaced in-place by VS Code's bundled `claude` binary, leaving stdin/stdout/stderr and signals completely untouched.

### 3. History, `--resume`, and Mid-Session Switching

- **Unified history:** Requires the default `~/.claude` account directory to be mirrored into `~/.cpool/shared` via `mirror.py`. Because VS Code's extension host reads the History sidebar from `~/.claude`, mirroring ensures all chats appear in one list regardless of which account spawned them.
- **Zero `--resume` routing logic:** Because `projects/` and `history.jsonl` are shared, `cpool-ide-exec` does not need to map `--resume <id>` back to the original account.
- **Proxy-free mid-session switching ("reopen to switch"):** If the active account hits a usage limit mid-chat, the user closes the chat and reopens it from VS Code's History panel. VS Code spawns a fresh `claude --resume <session_id>` via `cpool-ide-exec`, which routes the resumed session to the next account with the most headroom.

### 4. Phase 2 (Optional): Soft-Limit Warning in VS Code

If desired after Phase 1 ships:
- Pass `--plugin-dir <plugin>` and `CPOOL_RUN=<uuid>` alongside `CPOOL_IDE=1`.
- Update `cpool/plugin/hooks/register.ts` (`turn.complete`) to check `CPOOL_IDE === "1"`: when `sess.leave` triggers, emit a non-fatal warning telling the user to reopen the chat from History to switch accounts, instead of writing `${home}/handoff/${run}.json`.
- Optionally add a `cpool ide setup` CLI command that verifies `~/.claude` mirroring and prints (or writes) the `claudeCode.claudeProcessWrapper` entry for VS Code `settings.json`.

---

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| **Local HTTP proxy / shard mode (`ANTHROPIC_BASE_URL`)** | Violates the explicit no-proxy requirement; flags Claude Code as non-first-party (disabling some first-party features) and requires a long-running local proxy daemon. |
| **Static `claudeCode.environmentVariables` (`CLAUDE_CONFIG_DIR`)** | Works today with zero code, but pins a single account per VS Code window with no automatic headroom routing. |
| **Stdio proxy restarting `claude` mid-session** | Tightly coupled to undocumented VS Code ↔ CLI `stream-json` framing; fragile across extension updates. |
| **Mutating `.credentials.json` on disk mid-session** | Claude Code caches OAuth tokens in memory; disk swaps are unreliable and racy across concurrent sessions. |

---

## Consequences

### Positive
- **Zero proxy, 100% first-party `api.anthropic.com` traffic.**
- **Bundled in the `cpool` wheel:** installing/upgrading `cpool` automatically provides `cpool-ide-exec`.
- **Zero runtime overhead:** `os.execvpe` removes the wrapper before Claude initializes; VS Code's stdio stream is untouched.
- **No split history or `--resume` hacks:** leverages existing `mirror.py` symlinks.
- **Clean fallback for mid-session limits:** reopening a chat from History switches accounts seamlessly.

### Negative / Trade-offs
- **No transparent mid-turn failover:** hitting a limit mid-chat requires closing and reopening the chat from History (one manual action).
- **No terminal `statusLine` telemetry:** account headroom in IDE-only workflows depends on `cpool`'s OAuth usage poller (`poll_daemon` / `poll_due`) rather than live statusline ticks.
- **Webview-only:** `claudeCode.claudeProcessWrapper` applies to the sidebar webview panel, not `claudeCode.useTerminal: true` (where users run `cpool` directly in the terminal).
- **Prerequisite:** `~/.claude` must be mirrored via `cpool` so the extension host's History UI stays in sync with pooled accounts.

---

## Implementation Plan (Build Checklist)

1. Extract account-selection helper from `run.py:195-223` so `run.py` and `ide_exec.py` share one routing path.
2. Add `cpool/src/cpool/ide_exec.py` implementing passthrough detection, bounded usage refresh, routing, env setup (`CLAUDE_CONFIG_DIR`, `CPOOL_ACCOUNT`, `CPOOL_IDE=1`), and `os.execvpe`.
3. Register `cpool-ide-exec = "cpool.ide_exec:main"` in `cpool/pyproject.toml`.
4. Add unit tests in `cpool/tests/test_ide_exec.py` covering:
   - helper/non-session passthrough (`--version`, `plugin list`, etc.),
   - session routing + `CLAUDE_CONFIG_DIR` / `CPOOL_IDE` / unset `CPOOL_RUN`,
   - `--model` forwarding to `router.choose`,
   - fallback when no account is under thresholds or usage poll times out.
5. Document VS Code setup (`settings.json` snippet + `~/.claude` mirror prerequisite) in `cpool` README.
