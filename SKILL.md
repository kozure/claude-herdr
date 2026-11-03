---
name: claude-herdr
description: Shared contract for any agent driving herdr-managed Claude Code sessions directly (socket client + ledger). Use when spawning a Claude Code session from an agent, orchestrating Claude Code tasks, registering a bridge session, or working with herdr panes outside the TUI. The module's steward owns it; read before your first bridge call.
---

# claude-herdr — shared contract for driving herdr directly (Q1=(c), D5)

Any agent may drive herdr sessions directly. The module's **steward**
owns the bridge, ledger and notify paths; other agents are guests.
This is the machine-global contract: read it before your first call.

## Socket + client

- Socket: `~/.config/herdr/herdr.sock` (daemon-owned; the launchd label
  ships as `com.alfred.herdr`, server survives agent restarts).
- Python client: `herdr_client.py` in this repo (imported via
  `sys.path.insert` on your checkout) — NDJSON over Unix
  socket; methods: `ping / workspace_create / agent_start / agent_get /
  agent_list / agent_read / agent_prompt / pane_send_keys / pane_read /
  pane_close`. CLI equivalents: `herdr agent start|get|list|read|prompt`,
  `herdr status server`. Note: `herdr agents list` (plural) is NOT a
  valid command.

## Ledger tagging is mandatory (B3)

Every session you start must be registered in the bridge ledger with
**caller identity** — identity lives in the ledger, not the herdr CLI:

```python
import sys
sys.path.insert(0, "/path/to/claude_bridge")  # this repo
from ledger import Ledger
from herdr_client import HerdrClient

ledger = Ledger(); ledger.init_schema()
client = HerdrClient()
entry = ledger.register_session(agent_id="your-agent-name",
                                pane_id=pane_id, purpose="one line why",
                                cwd="/path/to/work")
```

`agent_id` is how the steward's tooling (polling, collection, notify,
accountability) finds your session. Unregistered sessions are orphans.

## Readiness rule

A session is ready for prompting only after **3 consecutive `idle`**
`agent_get` states (startup settling). Never prompt on first idle.

## Liveness habit rules (binding on all callers)

- **Pass a large explicit backstop `timeout` (foreground) or `task_timeout`
  (background) on every spawn and agent-task API call.** Omission is not a
  safety default: installed QwenPaw 2.2.2.post1 hard-kills the task mid-run
  at 600s foreground / 3600s background (`qwenpaw/constant.py` L226-L228).
  Size the backstop well past the task's worst-case envelope and re-check
  the constants when the QwenPaw version changes. (Supersedes 2026-10-30
  the earlier "never pass a timer; platform default is no timer" rule.)
- **The timer is a runaway backstop only; it is never treated as
  liveness.** Liveness stays with polling (`check_agent_task`,
  `agent_get`): slow is fine; silence is evidence, never a trigger.
- Silence only becomes a stuck-candidate when status is absent/unclear
  AND silence ≥ 30 min — and a stuck-candidate is never auto-killed;
  surface to the operator with evidence.

## Session ownership

**Never drive another agent's session.** You own the sessions you
registered with your `agent_id`; anything else is the steward's or another
agent's. No queue-jumping on the steward's job queue (the operator
preempts, nobody else).

## Reporting rule (binding) — the brief must say "report back"

A session that stops without reporting is invisible: the pane goes idle, the
ledger records it, and the spawner is never told what happened. That is the
case that produced this rule (TICKET-444 parent 2.0, 2026-10-10: the work was
finished, correct, and silent for hours because the hand-written brief never
asked for a report).

- `Bridge.send()` appends a **reporting block** (`REPORTING_BLOCK`,
  marked by `REPORTING_MARKER`) to every **paired** prompt. Do not remove it.
  If you compose a prompt yourself, either include your own explicit
  "when you stop, report through `qwenpaw_chat`" section or call
  `_with_reporting_contract(prompt, task_id, target_agent)` from `bridge.py`.
- The marker makes it idempotent: a prompt that already carries it is left
  alone, so you never get two reporting sections.
- The block is only appended when the session is paired (`mcp_pair` /
  `agent_id`) — without a paired MCP server there is no channel to report
  through.

## Completion watcher (the steward's backstop — guests need not run it)

The pane's own report is the primary path; the watcher is what catches a
silence when the report never arrives. The steward runs it from its
heartbeat file:

```bash
cd ~/path/to/claude_bridge && python3 watch.py
```

It polls every ledger task and announces only **terminal** states nobody has
been told about: `done`, `blocked`, `lost`. `running` is never a trigger
(silence is evidence, never a signal), `killed` is deliberate, and a `lost`
pane whose result was already collected is cleanup, not a silence.
De-duplication is per (task, state) in `ledger.notified_at` /
`notified_state`. `--dry-run` previews; `--baseline` reconciles history on a
first run without notifying.

## Worker-limit scope disclosure (N5)

The steward's queue cap (`liveness.toml [queue] worker_limit`, default 3)
binds **the steward's promoted jobs only**. Direct herdr usage via this
contract is **uncapped** — be a courteous guest: start only what you
actively use, close panes when done (`pane_close`), and never leave
`working` sessions unattended at the end of your turn.
