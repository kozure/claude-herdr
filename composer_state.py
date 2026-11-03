"""composer_state.py — shared seam: composer-side signals for a dispatch.

Frozen **by shape** in parent `0.0`, before the two writers that meet here start:
`1.0` implements the recogniser, `3.0` calls it from `Bridge.send()`. Nothing
here talks to herdr — it is a pure text rule over one raw pane read.

The direction of failure is deliberate and load-bearing. The accept path that
did not observe a transition requires *positive* evidence that the composer was
cleared, so every form this rule cannot positively classify is undecidable, and
undecidable is `R=false` — `pane_unreadable`, the one verdict that never writes
to the pane again. The rule can therefore only ever fail towards a bounded wait
and a surfaced blocker; it can never fail towards `no_submission_evidence`,
whose remedy is re-typing a prompt into a live session.

This module imports nothing from the package at module level, on purpose:
`bridge.py` imports it, and an import back would be a cycle that breaks in both
directions — whichever module is loaded first sees the other half-built. The
one constant it needs from `bridge` is therefore imported lazily, inside the
single function that needs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# --- Sentinel format -------------------------------------------------------

#: Per-dispatch sentinel line. Small and inert to the receiving session, and
#: short enough to survive the composer without wrapping on a real pane.
SENTINEL_TEMPLATE = "[verify-send:{}]"


def sentinel_for(task_id: str) -> str:
    """Return the per-dispatch sentinel line for `task_id`.

    The sentinel is **line 1 of the text actually submitted**, injected *after*
    `bridge._with_reporting_contract()` has appended the reporting block — that
    function appends (`prompt.rstrip() + "\\n\\n" + REPORTING_BLOCK`), so the text
    on the wire is `sentinel\\n<caller prompt>\\n\\n<reporting block>` and the
    sentinel stays on line 1.

    It carries the **full** `task_id`, so a dispatch's evidence ties back to its
    ledger row with nothing re-derived.

    It must not contain `REPORTING_MARKER`: that string is
    `_with_reporting_contract`'s idempotence key, and a sentinel carrying it
    would make the reporting contract look already-satisfied, silently dropping
    the reporting block from the prompt. The format is checked rather than
    trusted, so a later edit to the template cannot quietly break the contract.
    """
    from bridge import REPORTING_MARKER  # lazy: see the module docstring

    line = SENTINEL_TEMPLATE.format(task_id)
    if REPORTING_MARKER in line:
        raise ValueError("sentinel must not contain REPORTING_MARKER")
    return line


# --- Composer legibility ---------------------------------------------------

#: Seeded once, here. The recogniser (`1.0`) reads this tuple and never
#: re-decides the set at run time; extending it is a code change carrying its
#: own table-driven tests. Tokens are only ever consulted to make a reading
#: *less* trustworthy, so a token added here can only widen the fail-safe.
COLLAPSE_TOKENS: tuple[str, ...] = (
    "[Pasted",  # Claude Code paste-collapse bracket, e.g. "[Pasted text #1 +24 lines]"
    "…",        # in-bracket elision glyph
)


# --- Reading shape and recogniser ------------------------------------------


@dataclass(frozen=True)
class ComposerReading:
    """What one raw pane read says about this dispatch's composer line.

    Exactly five fields — the shape is frozen here so `3.0` never has to guess
    one. `T` (the transition outcome) is deliberately **not** among them: it is
    the value `_wait_working` returns once `3.0` changes it, so the acceptance
    predicate `T or (R and not P and M)` takes `T` from there and `P`/`R`/`M`
    from this reading — never both from the same place.

    Frozen because a reading is evidence: it records what was observed at one
    moment and is not re-interpreted afterwards.
    """

    sentinel_present: bool
    """`P` — the sentinel is present verbatim on an inspected composer line."""

    legible: bool
    """`R`'s composer term — true only for the two positively-classified forms
    (sentinel verbatim, or sentinel absent and unelided); false when the line is
    undecidable, which is the fail-safe direction."""

    marker_after_composer: bool
    """`M` — a `⏺`/`✻` marker appeared after the last `❯`. Corroboration only."""

    composer_check: Literal["ok", "unavailable"]
    """`ok` when raw terminal text was obtainable; `unavailable` when only
    `parse_result`'s `text` was available — capability recorded, never asserted."""

    signals_consulted: tuple[str, ...]
    """Which signals the rule actually read to reach this verdict, not which
    signals exist."""


def classify_composer(raw_text: str, sentinel: str) -> ComposerReading:
    """Recognise this dispatch's composer state in one raw pane read.

    Implemented by `1.0`: the legibility rule over `COLLAPSE_TOKENS`, the
    undecidable-by-default fail-safe, and the five-row decision table all live
    there. This parent freezes the signature only, so `3.0` can be written
    against it before the rule exists.

    `raw_text` is raw terminal text (or `parse_result(...)["raw_terminal"]`) —
    never `parse_result(...)["text"]`, which has the `❯` composer line stripped
    as a delimiter. `sentinel` is this dispatch's line from `sentinel_for()`.

    The rule, as a table:

    - no non-empty read                      -> `legible=False`, `unavailable`
    - no `❯` line in the read                 -> `legible=False` (nothing to classify)
    - any inspected line carries a
      `COLLAPSE_TOKENS` token, or a bracket
      that is not the sentinel itself        -> `legible=False` (undecidable)
    - otherwise                              -> `legible=True`

    The inspected lines are **every** `❯`-bearing line (not only the last —
    carried residual R2: the sentinel is line 1 of a multi-line composer, so
    a collapse form can sit on an earlier `❯` line), plus the continuation
    lines after the last `❯` up to the first `⏺`/`✻` marker, which are the
    composer's own wrapped/pasted lines. Any bracket left once the verbatim
    sentinel is removed is treated as undecidable: the seeded tokens name
    the collapse forms seen so far, and an unrecognised bracket (`[Image #1]`,
    a wrapped sentinel) is exactly the form the fail-safe must not trust.

    `P` is scoped more narrowly than that legibility term: the sentinel on the
    **last** `❯` line, or on that line's own continuation lines — never
    anywhere in the read. A real pane echoes a submitted prompt back into the
    transcript on its own `❯`-bearing line (observed on a live Claude Code pane
    2026-10-14: `❯ # SDD 01-verify-before-send — parent 3.0: …`, above a
    cleared composer), and that echo sits **above** the composer. Reading `P`
    over the whole text therefore made the predicate's ¬`T` marker-accept row
    unreachable in real rendering: a dispatch that *was* submitted, whose
    transition `T` was not sampled, was reported `composer_holds_prompt`
    instead of accepted. This is the 2026-10-14 remediation of sub-task `1.2`,
    conforming to the spec — `P` = the sentinel present on the last `❯`
    composer line. `R` keeps the wider R2 scope deliberately.
    """
    if not sentinel:
        raise ValueError("sentinel must be non-empty")
    if not raw_text.strip():
        return ComposerReading(False, False, False, "unavailable", ("raw_read",))

    lines = raw_text.splitlines()
    composer_idx = [i for i, line in enumerate(lines) if "❯" in line]
    if not composer_idx:
        # No composer line at all: nothing can carry `P`, and `R` is false, so
        # the read is `pane_unreadable` — never an accept, never a re-submit.
        return ComposerReading(
            False, False, False, "ok", ("raw_read", "sentinel", "composer_line")
        )

    # The composer region: the last `❯` line plus its own continuation lines, up
    # to the first response marker. `P` is read here and only here — see the
    # docstring for why the whole read is the wrong scope.
    region = [lines[composer_idx[-1]]]
    marker_after_composer = False
    for line in lines[composer_idx[-1] + 1:]:
        if "⏺" in line or "✻" in line:
            marker_after_composer = True
            break
        region.append(line)
    sentinel_present = any(sentinel in line for line in region)

    # Legibility keeps the wider scope (carried residual R2): every `❯`-bearing
    # line, plus the composer region's continuation lines.
    inspected = [lines[i] for i in composer_idx[:-1]] + region
    legible = not any(
        any(tok in line for tok in COLLAPSE_TOKENS)
        or "[" in line.replace(sentinel, "")
        or "]" in line.replace(sentinel, "")
        for line in inspected
    )
    return ComposerReading(
        sentinel_present,
        legible,
        marker_after_composer,
        "ok",
        ("raw_read", "sentinel", "composer_line", "marker"),
    )
