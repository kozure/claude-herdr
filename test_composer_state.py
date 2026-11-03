"""Tests for composer_state.classify_composer — the Composer legibility rule.

The rule is a table, so the tests are a table: one row per row of the spec's
legibility rule (Unit 2), then a fail-safe enumeration of composer forms the
rule does not positively recognise. The property that matters is directional:
no unrecognised form may yield a reading eligible for `no_submission_evidence`
(legible and sentinel-absent) — that verdict is the only one that writes to the
pane again. Every fixture is synthetic; no herdr, no Bridge.
"""
import pytest

from composer_state import COLLAPSE_TOKENS, ComposerReading, classify_composer

S = "[verify-send:task-00000000]"


def eligible(r: ComposerReading) -> bool:
    """A reading that could reach `no_submission_evidence` (given ¬T, ¬M)."""
    return r.legible and not r.sentinel_present


# --- one test per row of the legibility rule --------------------------------

LEGIBILITY_TABLE = [
    # id, raw read, P, legible
    ("sentinel-verbatim", f"⏺ earlier\n❯ {S}\n  do the thing\n", True, True),
    ("sentinel-absent-unelided", "⏺ earlier\n❯ \n", False, True),
    ("collapse-token", "❯ [Pasted text #1 +24 lines]\n", False, False),
    ("elided-line", "❯ [verify-send:task-0000…]\n", False, False),
    ("no-non-empty-read", "", False, False),
    # 1.2 remediation, 2026-10-14. A real pane echoes the submitted prompt into
    # the transcript on its own ❯ line, ABOVE the cleared composer (observed on
    # a live pane: `❯ # SDD 01-verify-before-send …`). P is scoped to the last
    # ❯ region, so the echo reads ¬P — which is what lets the ¬T marker-accept
    # row fire at all in real rendering.
    ("echo-above-cleared-composer",
     f"❯ {S}\n  caller prompt line 2\n⏺ done\n✻ Crunched\n❯ \n", False, True),
]


@pytest.mark.parametrize(
    "raw,p,legible", [r[1:] for r in LEGIBILITY_TABLE], ids=[r[0] for r in LEGIBILITY_TABLE]
)
def test_legibility_rule_row(raw, p, legible):
    r = classify_composer(raw, S)
    assert (r.sentinel_present, r.legible) == (p, legible)


@pytest.mark.parametrize("token", COLLAPSE_TOKENS)
def test_every_seeded_token_is_undecidable(token):
    r = classify_composer(f"❯ some text {token}\n", S)
    assert not r.legible


def test_no_non_empty_read_records_unavailable_and_reads_nothing_else():
    r = classify_composer("  \n\n", S)
    assert r == ComposerReading(False, False, False, "unavailable", ("raw_read",))


# --- P's scope (1.2 remediation, 2026-10-14) --------------------------------
#
# P is the sentinel on the LAST ❯ line (or that line's continuation lines),
# never anywhere in the read. The whole-read form made the ¬T marker-accept row
# unreachable on a real pane, because the transcript echo of a submitted prompt
# carries the sentinel above the cleared composer.

def test_sentinel_on_an_earlier_composer_line_is_not_p():
    """The exact regression: the transcript echo above the composer is not P.

    The markers sit ABOVE the last ❯ in this fixture because that is how a real
    pane renders (observed 2026-10-14, 12 reads over 6 live sessions): the input
    box is re-rendered last, so `M` reads false in this layout. Whether `M` can
    ever be true on a real pane is a separate, open question — it is recorded
    for 5.0's drill and deliberately not asserted or changed here.
    """
    r = classify_composer(f"❯ {S}\n  caller prompt\n⏺ On it.\n✻ Working…\n❯ \n", S)
    assert (r.sentinel_present, r.legible) == (False, True)


def test_sentinel_on_the_last_composer_line_is_still_p_with_an_echo_above():
    """Scoping P does not weaken the held-prompt signature (TICKET-209)."""
    r = classify_composer(f"❯ {S}\n⏺ On it.\n✻ Working…\n❯ {S}\n  still typed\n", S)
    assert r.sentinel_present
    assert r.legible


def test_sentinel_on_a_continuation_line_of_the_last_composer_is_p():
    """A wrapped composer is one composer: its continuation lines carry P."""
    r = classify_composer(f"❯ \n  {S}\n", S)
    assert r.sentinel_present
    assert r.legible


def test_echo_continuation_lines_are_not_inspected_for_legibility():
    """The echo's own continuation lines are not composer content: a bracket
    there must not make the reading undecidable."""
    r = classify_composer(f"❯ {S}\n  see [1] and {S}\n⏺ done\n❯ \n", S)
    assert (r.sentinel_present, r.legible) == (False, True)


# --- marker after composer (M) ----------------------------------------------

@pytest.mark.parametrize("marker", ["⏺", "✻"])
def test_marker_after_last_composer_line(marker):
    r = classify_composer(f"❯ {S}\n{marker} working on it\n", S)
    assert r.marker_after_composer


def test_marker_only_before_composer_is_not_m():
    r = classify_composer("⏺ old answer\n✻ Thinking\n❯ \n", S)
    assert not r.marker_after_composer


def test_marker_after_composer_does_not_make_response_text_illegible():
    # Response text after the marker is not composer content: its brackets
    # and ellipses must not be inspected.
    r = classify_composer("❯ \n⏺ see [Pasted text #1] and more…\n", S)
    assert r.legible and r.marker_after_composer


# --- signals_consulted -------------------------------------------------------

def test_signals_consulted_full_read():
    r = classify_composer("❯ \n⏺ ok\n", S)
    assert r.composer_check == "ok"
    assert r.signals_consulted == ("raw_read", "sentinel", "composer_line", "marker")


def test_marker_not_consulted_without_a_composer_line():
    r = classify_composer("⏺ just output\n", S)
    assert "marker" not in r.signals_consulted
    assert not r.marker_after_composer


def test_empty_sentinel_rejected():
    with pytest.raises(ValueError):
        classify_composer("❯ \n", "")


# --- fail-safe enumeration ---------------------------------------------------

FAIL_SAFE_FORMS = [
    ("unrecognised-bracket", "❯ [Image #1]\n"),
    ("unrecognised-bracket-with-marker", "❯ [Clipboard +3 lines]\n⏺ hi\n"),
    ("multi-line-paste-sentinel-wrapped", "❯ [verify-send:task-0000\n  0000] do the thing\n"),
    ("multi-line-paste-token-on-continuation", "❯ do the thing\n  [Pasted text #2 +9 lines]\n"),
    ("empty-read", ""),
    ("whitespace-only-read", "   \n\t\n"),
    ("no-composer-line", "⏺ output only\n✻ Thinking\n"),
    # Carried residual R2: the collapse token sits on a NON-last ❯ line.
    ("R2-token-on-non-last-composer-line", "❯ [Pasted text #1 +24 lines]\n⏺ x\n❯ \n"),
    ("R2-elision-on-non-last-composer-line", "❯ [verify-send:task…]\n❯ \n⏺ x\n"),
]


@pytest.mark.parametrize(
    "raw", [f[1] for f in FAIL_SAFE_FORMS], ids=[f[0] for f in FAIL_SAFE_FORMS]
)
def test_unrecognised_form_never_eligible_for_no_submission_evidence(raw):
    r = classify_composer(raw, S)
    assert not eligible(r)
    assert not r.legible
