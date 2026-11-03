# Claude Bridge (herdr-backed)

**v2 → v3 cutover complete.** Replaces `v2` (self-built async orchestrator + detached-process management) with a thin client backed by **herdr** (terminal session manager, v0.8.2, protocol 20). The herdr server provides process lifecycle, panes, and agent states natively — bridge manages scheduling and state on top of it.

## Quick Start

```python
from claude_bridge.bridge import Bridge
from claude_bridge.herdr_client import HerdrClient
from claude_bridge.ledger import Ledger

herdr = HerdrClient()                  # Connect to herdr server via socket
ledger = Ledger("claude_bridge/ledger.db")  # Create or load ledger
bridge = Bridge(herdr, ledger)

# Send a task (async — returns immediately after prompt is sent)
result = bridge.send("Summarize latest tech news", cwd="~/DEV")
task_id = result["task_id"]            # e.g. "task-abc12345"

# Check state / get results
poll = bridge.poll(task_id)             # Individual task
all_results = bridge.poll_all()         # All tasks at once ("any results back?")

# Multi-turn chat on an existing task
bridge.prompt(task_id, "Expand on point 3")

# Kill a running task
bridge.kill(task_id)                   # Returns early if already done
```

The bridge provides 6 methods: **send**, **poll**, **prompt**, **kill**, **poll_all**, and **reconcile**. Everything else (state mapping, terminal parsing, CAS on collected_at) is internal.

## herdr Setup

herdr is a local daemon providing terminal session management for Claude Code agents.

### Install

```bash
brew install luther/tap/herdr        # or: pip install herdr
```

### Configuration — `herdr_config.toml`

```toml
onboarding = false                   # Skip onboarding prompts for headless use

[experimental]
pane_history = true                  # Panes maintain full scrollback (for result parsing)

[session]
resume_agents_on_restore = true     # Agents auto-resume when herdr restarts
```

Store `herdr_config.toml` in `~/.config/herdr/` or pass via `-c <path>`.

### launchd Agent

herdr runs as a user-launchd agent. The plist is at:

```
claude_bridge/com.luther.herdr.plist
```

Typically the installer places it at:
```
~/Library/LaunchAgents/com.luther.herdr.plist
```

**Load / unload:**
```bash
launchctl load ~/Library/LaunchAgents/com.luther.herdr.plist
launchctl unload ~/Library/LaunchAgents/com.luther/herdr.plist
```

**Verify it's running:**
```bash
launchctl list | grep herdr          # Should show PID (0 = loaded, negative = crashed)
herd --version                       # e.g. herd 0.8.2 (or: herdr --version)
ls -la ~/.config/herdr/herdr.sock  # Socket path; verify existence
```

## State Mapping

The bridge translates between **herdr agent states** and **bridge logical states**:

| herdr `agent_status` | Bridge State | Meaning & Action |
|---|---|---|
| `idle` (no pending) | **idle** | Agent is resting; nothing to do |
| `idle` + `pending_result=true` / `last_state=working` | **running** | Prompt was sent but result not yet collected — collect the next complete response |
| `working` | **running** | Agent is processing — don't collect yet, wait for idle/done |
| `blocked` | **blocked** | Surface to the operator (launch dialogs are pre-empted by trust pre-seeding; see below) |
| herdr server down | **daemon_down** | Server unreachable; wait for restart then reconcile |
| Agent not found (lost pane) | **lost** | No recovery possible without restarting the agent |

### The `pending_result` Flag

When you send a prompt, the ledger sets:
- `last_state = working`
- `pending_result = true`
- `collected_at = NULL`, `cached_result = NULL` (for new tasks)

Any poll on this task will **collect** (even if you miss the working window), ensuring no result slips through. This eliminates the race condition where `poll()` fires between `working` and `idle`.

## CLI

**There is no `bridge.py` CLI.** `bridge.py` defines the `Bridge` class only
(no `main`), so the `python3 bridge.py send|poll|prompt|poll-all|kill|reconcile`
commands documented here previously in this section are not implemented —
running them does nothing (verified 2026-10-10). Use the programmatic API
(`python3 -c "from bridge import Bridge; ..."`, see the Quick Start above) or
the `herdr` CLI for pane-level work.

The one shipped command-line entry point is the completion watcher:

```bash
cd claude_bridge/

python3 watch.py             # poll every ledger task, announce new terminal states
python3 watch.py --dry-run   # show what would be announced; send nothing
python3 watch.py --baseline  # first run: reconcile pre-watcher history, notify nothing
python3 watch.py --json      # machine-readable summary
```

## Reporting Contract (brief fix, 2026-10-10)

A promoted session that stops without reporting is invisible: the pane goes
idle, the ledger records it, and nobody is told. `Bridge.send()` therefore
appends a reporting block to every **paired** prompt, so the brief can no
longer omit the request by accident:

- `REPORTING_MARKER` = `## Report back (required)`; the block names the
  session's `task_id`, the target agent, and the paired tool
  (`mcp__qwenpaw-bridge__qwenpaw_chat`), and lists the fields to report
  (task / status / changed / evidence / blockers / next).
- Idempotent: a prompt that already contains the marker is left unchanged.
- Paired only (`mcp_pair`, or any `agent_id`): without an MCP channel there
  is nothing to report through, so an unpaired send is untouched.
- The augmented prompt is what is sent **and** what the ledger stores, so the
  ledger shows exactly what the pane was told.

Helper: `_with_reporting_contract(prompt, task_id, target_agent)`.

## Verify-before-send — `HERDR_VERIFY_SEND` (SDD 01, off by default)

Without verification, `send()` records `working` as soon as `agent_prompt`
returns, whether or not the prompt ever left the composer (the TICKET-209 stall).
With the flag on, `send()` *observes* acceptance before recording it.

- **The flag:** read exactly as `os.environ.get("HERDR_VERIFY_SEND") == "1"`.
  Only the string `"1"` enables it; unset, `0`, `false`, `true` or anything
  else leaves it off. **Off is the default and is byte-identical to the
  pre-flag behaviour:** same wire text, same ledger writes, same return dict,
  same pre-send path.
- **Sentinel:** `[verify-send:<task_id>]` becomes line 1 of the wire text,
  after the reporting block is appended:
  `sentinel\n<prompt>\n\n<reporting block>`. The ledger's `prompt` column
  stores that wire text.
- **Signals:** `T` = `_wait_working` saw `working` (or `done`) inside its
  10 s window; `P`/`R`/`M` = `composer_state.classify_composer()` over a
  **raw** read-back (`pane_read(visible)`, falling back to
  `agent_read(recent_unwrapped)`; never `parse_result()["text"]`).
- **Predicate:** accepted iff `T or (R and not P and M)`.
- **Return fields (additive):** `verified` (bool); on acceptance
  `verified_by` ∈ `transition` · `marker`; on non-acceptance `reason` ∈
  `composer_holds_prompt` · `no_submission_evidence` · `pane_unreadable`;
  `evidence` = `{transition, composer_check (ok|unavailable),
  sentinel_present, marker_after_composer, signals_consulted}`.
- **Ledger:** the row is always written. Accepted: `last_state='working'`,
  `pending_result=1`. Not accepted: `last_state='blocked'`,
  `pending_result=0` — never `working`.
- **Failure semantics:** a non-accepted dispatch is **not** retried and the
  pane is not touched again; the caller gets the `reason` and `evidence`.
  (Retry shapes and the `blocked`-class error payload are parent 4.0.)

## Completion Watcher (watcher fix, 2026-10-10)

`watch.py` is the backstop for the case the report never arrives. It polls
every ledger task (`Bridge.poll_all()`) and announces only terminal states
nobody has been told about:

| state | announced? | why |
|---|---|---|
| `done` | yes | finished, result collected |
| `blocked` | yes | waiting on a human (dialog, decision) |
| `lost` | yes, unless a result was already collected | session vanished mid-flight |
| `running` | no | silence is evidence, never a trigger |
| `idle` / `unknown` | no | not terminal |
| `killed` | no | deliberate |

- De-duplication is per **(task, state)**: `ledger.notified_at` +
  `notified_state`, so a task announced `blocked` that later reaches `done` is
  announced again.
- Rows with no pane (`register_session` bookkeeping) are never announced.
- A notify that cannot be sent is reported in `failed` and left **unmarked**,
  so the next pass retries instead of swallowing it.
- `--baseline` reconciles pre-watcher history explicitly: it marks qualifying
  rows without notifying and prints what it reconciled, because the watcher
  post-dates the ledger and its first run would otherwise announce every
  stale row. (Baseline records the *real* state — the dedup key is
  (task, state) — otherwise the reconciled rows come straight back.)
- Needs the ledger columns `notified_at` / `notified_state`; `init_schema()`
  adds them on first use (backup-first, like the MCP columns).

## Result Extraction

The bridge reads terminal text from the herdr pane and parses Claude's response using **terminal markers** (⏺, ❯, ✻). The `result_parser.py` module implements this:

1. Find all `❯` prompt positions in the terminal text.
2. Walk backwards to find the last prompt that has a `⏺` response after it (before the next `❯`).
3. Extract the `⏺` block between that prompt and the next one (or end of text).
4. Strip ✻ status lines for clean output.

### Fallback Behavior

If no `❯`/`⏺` markers are found, the parser returns the raw terminal text with a `parse_warning = "markers_not_found"` flag. This can happen with:
- Very short sessions (no prompt marker emitted)
- Non-interactive sessions (`--bare` mode)
- Claude CLI bugs or UI changes

In v3, the parser also handles the **trailing idle prompt** case — after Claude completes a response, a new `❯` idle prompt appears at the bottom with no response text. The parser correctly identifies the last complete response by looking for `⏺` markers across all prompts (walks backwards).

## Trust Pre-seeding (replaces the old keystroke heuristic)

Claude Code pauses on launch in an untrusted directory with **"Do you trust the
files in this folder?"**. **There is no CLI flag and no environment variable that
suppresses it** — verified empirically against 2.1.285 (`--dangerously-skip-permissions`,
`--permission-mode bypassPermissions`, `CLAUDE_BG_WORKSPACE_TRUSTED`,
`CLAUDE_CODE_RELAUNCH_HOME_TRUST` all still show the dialog; `claude -p` skips the
dialog but never records acceptance). Trust is persisted *state*, and Claude Code's
own error message documents the supported non-interactive path:

> …or set `projects["<path>"].hasTrustDialogAccepted: true` in `~/.claude.json`

So `Bridge.send()` calls `trust.preseed_trust(cwd)` **before** `workspace_create`,
marking the launch directory trusted so no dialog ever appears.

### Two gates, both required

| Key | Dialog it suppresses |
|---|---|
| `hasTrustDialogAccepted` | "Do you trust the files in this folder?" |
| `hasClaudeMdExternalIncludesApproved` | "Allow external CLAUDE.md file imports?" |

Both live in the same project entry and **both must be set** or the launch still
hangs. The imports dialog fires when a `CLAUDE.md` contains an `@`-import pointing
outside its directory tree — e.g. `~/DEV/CLAUDE.md` line 5
(`@CONTEXT.md`) trips it for every cwd under `DEV`.

### Rules that make a naive implementation fail

- **Keys are realpath-normalised.** macOS `/tmp` → `/private/tmp`, trailing slashes
  stripped. Writing the unresolved path silently does nothing (no error).
- **The dialog gate cascades to parent dirs but stops at the git root**; the settings
  gate is an exact match with no inheritance. Since `git init`'d projects are the
  normal case, parent-trust alone fails — the exact launch directory must be trusted.
  That is fine, because the bridge launches at a known `cwd`.
- **`$HOME` trust is never persisted** by Claude Code (it resets to `false`).
- **Merge, never replace.** Claude Code rewrites `~/.claude.json` continuously (live
  counters such as `numStartups`); the write is therefore an atomic read-modify-write
  via temp file + `os.replace`, preserving file mode (0600 — the file holds
  credentials). An unparseable config is reported as an error and **left untouched**.

### Residual-dialog fallback (rarely reached)

If a directory is created *between* pre-seeding and launch, a dialog may still
appear. `_residual_dialog_answer()` matches the two dialogs by explicit content
markers and returns the correct keys — never blind keystrokes:

- **Trust dialog** — default focus is `No, exit Claude Code`, so a bare Enter
  *kills the session*. Answer: `Down`, `Enter`.
- **Imports dialog** — default focus is already `Yes, allow`; `Down` would move to
  `No`. Answer: `Enter` alone.

Anything unrecognised returns `None` and the bridge surfaces it to the operator rather than
firing keys into a live REPL.

### Risk note

Trusting a directory is not "dismissing a dialog" — it switches **on** that repo's
`.claude/settings.json` permissions, **hooks** (arbitrary code execution) and project
**MCP servers**. Pre-seeding is therefore scoped to the launch `cwd` (which the
bridge restricts to an allowed root — `ALLOWED_CWD` or an agent workspace), not to parents or `$HOME`.

**F11 Tripwire:** For automation scripts (headless usage), the bridge enforces `--no-trusted-warnings` (or F11 bypass) when starting Claude Code, avoiding interactive prompts that would block execution. This is recommended for cron or automated tasks.

## Restart Behavior

When herdr restarts:
- **herdr auto-resumes** all agents natively (per `resume_agents_on_restore = true`). No extra work needed.
- The bridge's ledger entries persist across restarts — no cleanup required.
- Agents transition back to their previous state (`working` → idle when complete).

To ensure robust recovery: **reconcile** the ledger after a restart to check task states. The `bridge.reconcile()` method syncs the ledger against live herdr agent state and can auto-start agents for stale entries.

## herdr-Down Behavior

If the herdr server goes down, bridges immediately detect it (no socket) and return a **`daemon_down`** error with an informative message — no crash or hang. The scheduler can retry polls periodically, and when herdr comes back online:
1. Run `bridge.reconcile()` to sync ledgers.
2. Any tasks previously marked `daemon_down` will resume normally (auto-resume).

For production automation, add a simple watchdog that monitors the socket path (`~/.config/herdr/herdr.sock`) or pings herdr periodically before polling. If the server is down, skip polling and retry after a brief interval.

## Timeout Semantics

The `timeout_min` field (set in `send()`) is **advisory only** — it stores the timeout value in the ledger but does not trigger an automatic kill. The bridge does not auto-kill on timeout. This keeps the logic simple and avoids false positives from long-running but responsive agents. Manual `bridge.kill()` is used to terminate sessions.

## Acceptance Criteria

Verify the bridge works correctly:
1. ✅ Send async task → 3 consecutive idle confirmations before prompt sent
2. ✅ `poll --all` returns mixed states mid-run (running/idle)
3. ✅ Done state carries correct parsed result text from herdr pane
4. ✅ Cached re-poll same result (no re-read overhead)
5. ✅ Launch dialogs pre-empted by trust pre-seeding (no keystrokes sent)
6. ✅ `daemon_down` error clear and non-panic-inducing when herdr is down
7. ✅ Multi-turn prompt correctly resets collection state for next poll

## Cutover Prerequisites and Post-Cutover Steps

### Before cutover (v2 → v3):
1. Verify herdr is installed:
```bash
# herd --version   # e.g. 0.8.x with protocol ~20
```
2. Ensure legacy ledger files are backed up if needed — the new `ledger.db` format uses the same SQLite backend.
3. Stop any running bridge sessions (`claude_bridge/stale-pid*.lock` or similar), then stop v2 processes:
```bash
# kill -SIGTERM <v2_bridge_pid>
```

### After cutover:
1. Restart with launchd:
```bash
launchctl unload ~/Library/LaunchAgents/com.luther/herdr.plist   # If upgrading
launchctl load ~/Library/LaunchAgents/com.luther/herdr.plist     # Always load new version
```
2. Validate the socket path (`herd --socket` or `~/.config/herd/herd.sock`).
3. Test a single async task:
```bash
python3 -c "from claude_bridge.bridge import Bridge; ... bridge.send('test')"
```
4. Ensure all scheduled jobs point to `v3` endpoints/config.
5. Roll back plan below if anything fails mid-test.

## Migration Notes

**Replaces:** v2 with self-built async orchestrator, detached-process management, and custom state machine.

**New stack (v3):** herdr terminal session manager + thin bridge layer. The CLI interface remains similar but now routes through herdr rather than managing processes directly. Agents run in detached panes (panes are the primary runtime unit for Claude instances).

## Rollback Procedure

If v3 is unstable or breaking:
1. **Stop new bridge processes** (`herd`/`herd-cli`).
2. **Restart v2:**
```bash
launchctl start com.luther/herdr-bridge          # If v2 had systemd service
# Or restore the old startup command from backup
cd /path/to/v2/clande_bridge_dir && python3 bridge_v2.py
```
3. Ensure v2 ledger is intact (backup before cutover).

## State Mapping for Users

- **idle**: Task running, no result to collect yet
- **running**: Prompt was sent but result not collected — poll will collect it
- **done**: Complete with result text from terminal parsing
- **blocked**: Agent blocked on an unrecognised prompt (launch dialogs are pre-empted by trust pre-seeding; only show if persisting)
- **daemon_down**: herdr is down; wait and reconcile when it restarts
- **lost**: Agent lost — no state available, may need manual investigation

## Notes

- **CWD Rule:** Sessions may be spawned under `~/DEV` (the default) or an agent workspace under `~/qwenpaw/workspaces/workspaces/`. Any other cwd is refused. Always pass cwd: `bridge.send("task", cwd="~/DEV")`.
- **Pro Plan OAuth:** The bridge uses Claude CLI Pro plan interactive mode — no API keys or raw tokens.
- **Manual Only:** This skill is invoked manually (no auto-triggers). Use for async/convenience access to Claude Code sessions.
- **Pane History:** herdr panes maintain full scrollback history, which the bridge uses for result parsing.

## Timer and Watchdog — `timer.py` (SDD 02, launchd unit installed 2026-10-14)

`timer.py` fires scheduled `jobs` rows (`schedule.py`, a new table in the
ledger's own DB at `~/.local/share/claude_bridge/ledger.db`) and walks the
stuck-session escalation ladder. Two paths, never crossed: **firing** is a
due-time decision; **escalating** is a probe-result decision. The watchdog can
re-arm a row or raise a notification; it can never fire one.

### Two modes

```bash
cd claude_bridge
python timer.py --tick    # one-shot: fire what is due, print one JSON summary, exit
python timer.py --list    # show due / firing / failed rows (the backlog, not history)
```

Exactly one of `--tick` / `--list` is required. Two more flags exist:

- `--allow-spend` — **operator-only.** Lets this tick dispatch `send` / `resume`
  (paid Claude work). Never in the plist.
- `--no-watch` — skip the best-effort `watch.py` pass.

A `--tick` prints one JSON object with exactly these keys, always all of them:
`claimed`, `fired`, `rearmed_lease`, `spend_gate`, `locked`, `probe_miss`,
`stuck_candidates`, `lease_rearm_count`. **A tick is not an alarm:** a failing
action, an unreadable pane, a missing config or a locked DB is recorded on the
row and in the summary, and the tick still exits 0. Only operator errors (bad
arguments, an unopenable DB) exit non-zero.

### The launchd unit — installed 2026-10-14

```
claude_bridge/com.alfred.bridge-timer.plist
```

`Label` `com.alfred.bridge-timer`; `ProgramArguments`
`~/qwenpaw/qwen_env/bin/python …/claude_bridge/timer.py --tick`
(absolute paths, launchd has no shell); `StartInterval` 60; `RunAtLoad` true;
stdout and stderr both to `~/.local/share/claude_bridge/bridge-timer.log`;
`WorkingDirectory` the `claude_bridge/` directory. No `KeepAlive`: `--tick` is a
one-shot, launchd re-runs it every 60 s.

**Installed 2026-10-14** on the operator's explicit go — the gate this section used to
hold closed. It is installed **by copy**, so a change to the plist in the repo
must be re-copied and re-bootstrapped before launchd sees it:

```bash
cp claude_bridge/com.alfred.bridge-timer.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.alfred.bridge-timer.plist   # install
launchctl print gui/$(id -u)/com.alfred.bridge-timer | grep -E 'runs =|last exit code'
launchctl list | grep bridge-timer    # `-  0  com.alfred.bridge-timer` = loaded, last run exited 0
launchctl bootout gui/$(id -u)/com.alfred.bridge-timer                                  # uninstall
```

Each tick appends one line to `~/.local/share/claude_bridge/bridge-timer.log` —
the full 8-key JSON summary — and stays all-zero while no `jobs` row is due.

The plist's `ProgramArguments` carries **no `--allow-spend`**, and
`test_timer_plist.py` asserts that by parsing the file. That absence is what
bounds the unattended unit: it can notify and escalate, it cannot start a
session.

### The spend gate — an ordering, not an outcome

`send` and `resume` start paid work. Without `--allow-spend` the gate is
evaluated **before the claim**, so a gated job:

- is **never claimed** — its `status` stays `due`;
- keeps `attempts` **untouched** (0) — a deferral is not a failure;
- is neither failed nor skipped;
- gets `last_error='spend_gate'` written **once, on the first deferral**; every
  later tick only counts it in the summary's `spend_gate` (zero writes).

`attempts` counts failures of the action and nothing else; a spend-gate
deferral and a probe miss both re-arm without touching it. Gated rows are
visible in `--list`. To dispatch them, run one attended tick:
`python timer.py --tick --allow-spend`.

### Leases — `LEASE_MIN_S`, `ACTION_WORST_CASE_S`, `UNBOUNDED`

A claimed row goes `firing` with a lease; a `firing` row older than its lease
is re-armed to `due` at the start of the next tick (`rearmed_lease`).

- `LEASE_MIN_S = 120` s (2 × `StartInterval`).
- Each action declares its worst case in `ACTION_WORST_CASE_S`
  (`notify` 60, `snapshot` 30, `resume` 30, `send` `UNBOUNDED`); an action with
  no declaration is refused at `add_job`. A row's lease is
  `max(LEASE_MIN_S, worst_case_s)`.
- **`UNBOUNDED` ⇒ the lease *is* `LEASE_MIN_S`.** No `max()` is computed over an
  uncomputable maximum. A stranded unbounded `send` therefore re-arms like any
  other row — but its re-dispatch cannot happen unattended: the spend gate
  defers it on **every** later tick. Its recovery path is the **attended
  `--allow-spend` tick**, the moment a human can decide whether the earlier
  dispatch actually landed (re-dispatch is at-least-once: a duplicate prompt is
  possible).
- A re-armed row carries `last_error='lease_rearm'`. It means the previous
  `mark_fired` **did not land** — not that the action never ran. A
  caught-and-skipped `mark_fired` (DB locked) is itself a re-fire source and is
  counted the same way in `lease_rearm_count`.

### Escalation policy — probe-result only, never kill

Elapsed time decides only **when to look** (first look once a job is
`warn_after_s` old, a confirming look at `stuck_after_s`); the **probe result**
decides what to conclude. `alive` never escalates, however long the machine
slept. Only `due` rows are watched; `fired` / `cancelled` rows are never probed;
`probe = 0` on a row opts it out.

The 9-row input mapping (`schedule.classify`, total over any status string):

| Row | `agent_status` (+ composer) | Verdict | Reason |
|---|---|---|---|
| 1 | `working` | `alive` | — |
| 2 | `done` (herdr: turn finished) | `alive` | — |
| 3 | `blocked` | `miss` | `blocked_prompt` |
| 4 | `killed` (ledger task state) | `miss` | `pane_gone` |
| 5 | `idle`, composer legible, no sentinel | `alive` | — |
| 6 | `idle`, composer holds the prompt | `miss` | `composer_holds_prompt` |
| 7 | `idle`, composer unreadable | `undecidable` | `pane_unreadable` |
| 8 | `None` / `unknown` (`AgentNotReady`) | `undecidable` | `agent_not_ready` |
| 9 | any other (novel) string | `undecidable` | `unknown_status` |

The composer is read **only** when `agent_status == 'idle'`. A novel status is
never `alive`, so a vocabulary change cannot silently suppress an escalation.
The reason names are c1's (`HERDR_VERIFY_SEND`), one vocabulary across both.

The ladder:

1. **First** non-`alive` (`miss` or `undecidable`) ⇒ row back to `due` with a
   `probe_miss` / `probe_undecidable` annotation in `last_error`; **nobody is
   notified.**
2. **Second consecutive** non-`alive` ⇒ one **stuck-candidate** notification
   through the existing `notify.py` path, carrying both probe results in
   order, the thresholds in force, the reason, and the options (inspect the
   pane; submit or clear the composer by hand; cancel the job; kill the session
   only on the operator's explicit approval).
3. A latch (`last_error='stuck_candidate'`) makes that **once per incident**: a
   permanently stalled pane notifies once, not every 60 s.
4. `alive` clears both the counter and the latch; a later stall is a new
   incident.

**Nothing is ever killed, closed, restarted, resumed or re-spawned.** The
watchdog's only writes are to its own `jobs` rows; the worst case of a
spurious candidate is one notification a human reads. Known edge: a slow agent
start can read as two consecutive `AgentNotReady` and raise one spurious
candidate.

**Wiring caveat (2026-10-14, open with parent 4.0):** `tick()` runs the
watchdog only when one is passed in, and `timer.py`'s `main()` passes none by
default — so a plain `timer.py --tick` currently fires due jobs but does not
probe. The policy above is what `schedule.watchdog` implements once wired.

### Thresholds — `CLAUDE_BRIDGE_LIVENESS_TOML` (unset by default)

Thresholds resolve per row as `row value ?? liveness.toml ?? restated defaults`.

- **The variable:** `CLAUDE_BRIDGE_LIVENESS_TOML` names the shared
  `liveness.toml` by path. Unset, the path is
  `~/qwenpaw/workspaces/workspaces/default/liveness/liveness.toml`. Keys read:
  `[tiers] warn_after_s`, `[tiers] stuck_after_s`, `[probe] deadline_s`.
- **Fallback:** an absent or unreadable file degrades to `RESTATED_LIVENESS`
  (`warn_after_s` 900, `stuck_after_s` 1800, probe `deadline_s` 5). The
  degradation is **recorded** (the resolved thresholds carry a `fallback`
  string), never silent and never raised.
- **Per row:** the `warn_s` / `stuck_s` / `probe` columns override both.
- The probe deadline is passed to `HerdrClient` through its existing `timeout`
  kwarg, and a timeout becomes row 7 / row 8 rather than an exception.
  `herdr_client.py` is not modified by this feature.

### Known coexistence limits

- **Same DB, different lock habits.** The `jobs` table lives in the ledger's
  own `ledger.db`. `schedule.py` opens every connection with `timeout=5.0`
  (SQLite `busy_timeout` 5000 ms); a lock that outlives it is caught, counted
  in `locked`, the row is left exactly as found, and the tick exits 0. The
  live `ledger.py` writer reaches the same file through a bare
  `sqlite3.connect(self.db_path)` (`ledger.py:54`) with no explicit `timeout`.
  The spec describes that as `busy_timeout = 0`; measured on this host, a bare
  connect gets Python's implicit default (`PRAGMA busy_timeout` → 5000 on both
  3.12.14 and 3.14.7). Either way the ledger's lock posture is not pinned by
  the ledger, and real two-writer contention is not reproducible in-house, so
  the first sign of it in production would be a `database is locked` from a
  `ledger.py` caller. c2's exposure is bounded by design: single short
  statements, each committed before the next step, and **no write transaction
  held across a dispatch**. The remedy — an explicit `timeout=` on
  `ledger.py`'s connect — is a `ledger.py` change owned by a later item.

## License

MIT — see [LICENSE](LICENSE).
