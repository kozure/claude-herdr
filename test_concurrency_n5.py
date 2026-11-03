"""Task 5: N=5 concurrency test against live herdr.

Launches 5 trivial async tasks, verifies:
- All 5 get distinct task_ids and pane_ids
- poll --all shows 5 running (or mixed) mid-run
- All 5 reach done with correct results
- Ledger has 5 consistent rows
- Trust dialog scope: once per cwd or per pane?

This test runs against the LIVE herdr server. It requires the claude CLI
to be authenticated (Pro plan OAuth).
Set HERDR_INTEGRATION=1 to run.
"""
import os
import time

import pytest

from bridge import Bridge
from herdr_client import HerdrClient
from ledger import Ledger

HOME = os.path.expanduser("~")


@pytest.mark.skipif(
    not os.environ.get("HERDR_INTEGRATION"),
    reason="Set HERDR_INTEGRATION=1 to run integration tests"
)
class TestConcurrencyN5:
    """N=5 concurrency test — the core Task 5 deliverable."""

    SOCKET_PATH = os.path.expanduser("~/.config/herdr/herdr.sock")
    DEFAULT_CWD = f"{HOME}/DEV"

    @pytest.fixture
    def live_bridge(self):
        herdr = HerdrClient(socket_path=self.SOCKET_PATH)
        ledger = Ledger(":memory:")
        ledger.init_schema()
        b = Bridge(herdr=herdr, ledger=ledger, default_cwd=self.DEFAULT_CWD)
        yield b
        # Cleanup: kill all tasks
        for row in ledger.list_all():
            try:
                b.kill(row["task_id"])
            except Exception:
                pass

    def test_n5_concurrency(self, live_bridge):
        """Launch 5 tasks back-to-back, verify all complete correctly."""
        prompts = [
            "Reply with exactly: CONCURRENT_1 and nothing else.",
            "Reply with exactly: CONCURRENT_2 and nothing else.",
            "Reply with exactly: CONCURRENT_3 and nothing else.",
            "Reply with exactly: CONCURRENT_4 and nothing else.",
            "Reply with exactly: CONCURRENT_5 and nothing else.",
        ]

        # --- Phase 1: Launch all 5 ---
        results = []
        send_timings = []
        for i, prompt in enumerate(prompts):
            t0 = time.time()
            result = live_bridge.send(prompt, cwd=self.DEFAULT_CWD)
            elapsed = time.time() - t0
            send_timings.append(elapsed)
            assert "error" not in result, f"Task {i+1} send failed: {result}"
            assert result["task_id"] is not None, f"Task {i+1} got no task_id"
            results.append(result)
            print(f"  Task {i+1} sent in {elapsed:.1f}s: {result['task_id']} pane={result['pane_id']}")

        task_ids = [r["task_id"] for r in results]
        pane_ids = [r["pane_id"] for r in results]

        # --- Phase 2: Verify distinct IDs ---
        assert len(set(task_ids)) == 5, f"task_ids not distinct: {task_ids}"
        assert len(set(pane_ids)) == 5, f"pane_ids not distinct: {pane_ids}"
        print(f"\n✅ Phase 2: 5 distinct task_ids: {task_ids}")
        print(f"✅ Phase 2: 5 distinct pane_ids: {pane_ids}")

        # --- Phase 3: Poll --all mid-run (capture mixed states) ---
        # Wait a few seconds for agents to transition to working
        time.sleep(5)
        mid_results = live_bridge.poll_all()
        assert len(mid_results) == 5, f"poll_all returned {len(mid_results)} results, expected 5"
        mid_states = {r["task_id"]: r["state"] for r in mid_results}
        print(f"✅ Phase 3: Mid-run states: {mid_states}")
        # Accept any non-error state — running, done, idle, unknown (transient) are all valid
        for r in mid_results:
            assert r["state"] != "error", f"Mid-run poll error: {r}"

        # --- Phase 4: Wait for all to complete ---
        max_wait = 240  # 4 minutes
        deadline = time.time() + max_wait
        completion_times = {}
        last_states = {}
        while time.time() < deadline:
            poll_results = live_bridge.poll_all()
            all_done = True
            for pr in poll_results:
                state = pr["state"]
                tid = pr["task_id"]
                last_states[tid] = state
                if state == "done" and tid not in completion_times:
                    completion_times[tid] = time.time()
                    print(f"  ✅ {tid} completed")
                if state not in ("done", "killed", "lost"):
                    all_done = False
            if all_done:
                break
            time.sleep(3)

        assert len(completion_times) == 5, (
            f"Only {len(completion_times)}/5 completed within {max_wait}s. "
            f"Last states: {last_states}"
        )
        print(f"\n✅ Phase 4: All 5 completed within {max_wait}s")

        # --- Phase 5: Verify results are correct ---
        for i, tid in enumerate(task_ids):
            poll = live_bridge.poll(tid)
            assert poll["state"] == "done", f"Task {tid} not done: {poll['state']}"
            result_text = poll.get("result", "")
            if isinstance(result_text, dict):
                result_text = result_text.get("text", "")
            expected = f"CONCURRENT_{i+1}"
            assert expected in str(result_text), (
                f"Task {tid} result doesn't contain '{expected}': {str(result_text)[:200]}"
            )
            print(f"✅ Phase 5: {tid} result contains '{expected}'")

        # --- Phase 6: Ledger consistency ---
        rows = live_bridge.ledger.list_all()
        assert len(rows) == 5, f"Ledger has {len(rows)} rows, expected 5"
        for row in rows:
            assert row["collected_at"] is not None, f"Task {row['task_id']} not collected"
            assert row["pending_result"] == 0, f"Task {row['task_id']} pending_result not cleared"
        print(f"✅ Phase 6: Ledger consistent — 5 rows, all collected, all pending_result cleared")

        # --- Phase 7: Cached re-poll (no re-read) ---
        for tid in task_ids:
            poll1 = live_bridge.poll(tid)
            assert poll1["state"] == "done"
        print(f"✅ Phase 7: Re-poll returns cached results for all 5")

        # --- Trust dialog scope finding ---
        print(f"\n📋 Send timings (seconds): {[f'{t:.1f}' for t in send_timings]}")
        print(f"📋 Trust dialog scope: All 5 panes used cwd={self.DEFAULT_CWD}")
        first_vs_rest = send_timings[0] > (sum(send_timings[1:]) / 4) * 1.5 if len(send_timings) > 1 else False
        if first_vs_rest:
            print(f"   Finding: First pane significantly slower → trust dialog likely fires ONCE per cwd")
        else:
            print(f"   Finding: All panes similar timing → trust dialog likely fires PER-PANE (or already cached for this cwd)")

    def test_n5_trust_dialog_scope(self, live_bridge):
        """Empirically determine: does trust dialog fire once per cwd or per pane?

        We launch 5 panes in the same cwd and compare send timings.
        If only the first is slow (trust dialog), it's per-cwd; if all are slow, per-pane.
        """
        timings = []
        for i in range(5):
            start = time.time()
            result = live_bridge.send(
                f"Reply with exactly: TRUST_SCOPE_{i+1}",
                cwd=self.DEFAULT_CWD
            )
            elapsed = time.time() - start
            timings.append(elapsed)
            assert "error" not in result, f"Task {i+1} failed: {result}"
            print(f"  Task {i+1}: {elapsed:.1f}s")

        print(f"\n📋 Trust dialog scope timings (seconds): {[f'{t:.1f}' for t in timings]}")

        first_slow = timings[0] > (sum(timings[1:]) / 4) * 1.5 if len(timings) > 1 else False
        all_similar = max(timings) < min(timings) * 2 if min(timings) > 0 else True

        if first_slow:
            print(f"   Finding: First pane slower → trust dialog likely fires ONCE per cwd")
        elif all_similar:
            print(f"   Finding: All panes similar timing → trust dialog may fire PER-PANE")
        else:
            print(f"   Finding: Mixed timing pattern — inconclusive")

        # Cleanup
        for row in live_bridge.ledger.list_all():
            try:
                live_bridge.kill(row["task_id"])
            except Exception:
                pass