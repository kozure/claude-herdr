"""Tests for queue.py — filesystem job intake queue (plan Task 4).

Protocol: jobs/YYYYMMDD-HHMMSS-<slug>.md, STATUS: line
(queued/active/held/done), FIFO by filename, one active job,
worker_limit cap on promoted herdr sessions (N5: Alfred-side cap).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import job_queue
from job_queue import (claim_next, count_active_sessions, create_job,
                       list_jobs, load_worker_limit, mark_done, mark_held)


@pytest.fixture
def jobs(tmp_path):
    d = tmp_path / "jobs"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------

class TestCreateJob:
    def test_writes_markdown_with_status_queued(self, jobs):
        p = create_job(jobs, "fix-bridge", "Do the thing.")
        assert os.path.exists(p) and os.path.basename(p).endswith("-fix-bridge.md")
        text = open(p).read()
        assert "STATUS: queued" in text
        assert "Do the thing." in text

    def test_filename_second_resolution(self, jobs):
        p = create_job(jobs, "a", "x")
        stem = os.path.basename(p)[:-3]  # YYYYMMDD-HHMMSS-a
        date, clock, slug = stem.split("-")
        assert len(date) == 8 and len(clock) == 6

    def test_privileged_fields_recorded(self, jobs):
        p = create_job(jobs, "handoff", "x", privileged=True,
                       source_agent="deep_researcher")
        text = open(p).read()
        assert "PRIVILEGED: yes" in text
        assert "source_agent: deep_researcher" in text


# ---------------------------------------------------------------------------
# list_jobs — FIFO
# ---------------------------------------------------------------------------

class TestListJobs:
    def test_fifo_by_filename(self, jobs):
        create_job(jobs, "first", "x")
        create_job(jobs, "second", "x")
        names = [j.slug for j in list_jobs(jobs)]
        assert names == ["first", "second"]  # FIFO = filename order

    def test_same_second_tiebreak_lexicographic(self, jobs):
        # force identical timestamps
        p1 = jobs / "20260830-120000-beta.md"
        p1.write_text("STATUS: queued\n")
        p2 = jobs / "20260830-120000-alpha.md"
        p2.write_text("STATUS: queued\n")
        names = [j.slug for j in list_jobs(jobs)]
        assert names == ["alpha", "beta"]  # deterministic lexicographic

    def test_excludes_done_dir(self, jobs):
        os.makedirs(os.path.join(jobs, "done"), exist_ok=True)
        open(os.path.join(jobs, "done", "20260830-000000-old.md"), "w").write("STATUS: done\n")
        create_job(jobs, "live", "x")
        assert [j.slug for j in list_jobs(jobs)] == ["live"]


# ---------------------------------------------------------------------------
# claim — one active at a time
# ---------------------------------------------------------------------------

class TestClaim:
    def test_claim_marks_active_and_returns_job(self, jobs):
        create_job(jobs, "one", "x")
        job = claim_next(jobs)
        assert job.slug == "one"
        assert "STATUS: active" in open(job.path).read()

    def test_claim_picks_fifo_first(self, jobs):
        create_job(jobs, "first", "x")
        create_job(jobs, "second", "x")
        assert claim_next(jobs).slug == "first"

    def test_refuses_second_claim_while_active(self, jobs):
        create_job(jobs, "one", "x")
        claim_next(jobs)
        create_job(jobs, "two", "x")
        assert claim_next(jobs) is None

    def test_claim_skips_held(self, jobs):
        create_job(jobs, "held-one", "x")
        mark_held(jobs, "held-one", "waiting on the operator")
        create_job(jobs, "next", "x")
        assert claim_next(jobs).slug == "next"

    def test_no_jobs_returns_none(self, jobs):
        assert claim_next(jobs) is None


# ---------------------------------------------------------------------------
# worker_limit (Q4) — cap on promoted herdr sessions
# ---------------------------------------------------------------------------

class TestWorkerLimit:
    def test_refusal_when_cap_reached(self, jobs):
        create_job(jobs, "one", "x")
        assert claim_next(jobs, worker_limit=3, active_sessions=3) is None
        # refusal must not mutate the file
        assert "STATUS: queued" in \
            open(list_jobs(jobs)[0].path).read()

    def test_refusal_is_over_cap_not_equal_below(self, jobs):
        create_job(jobs, "one", "x")
        assert claim_next(jobs, worker_limit=3, active_sessions=2) is not None

    def test_cap_disabled_when_limit_none(self, jobs):
        create_job(jobs, "one", "x")
        assert claim_next(jobs, worker_limit=None,
                          active_sessions=99) is not None

    def test_count_active_sessions_uses_agent_list(self):
        class FakeClient:
            def agent_list(self):
                return [{"id": "a"}, {"id": "b"}]
        assert count_active_sessions(FakeClient()) == 2

    def test_count_active_sessions_tolerates_errors(self):
        class DeadClient:
            def agent_list(self):
                raise RuntimeError("herdr down")
        assert count_active_sessions(DeadClient()) == 0


# ---------------------------------------------------------------------------
# hold / done
# ---------------------------------------------------------------------------

class TestHoldAndDone:
    def test_mark_held_sets_status_and_reason(self, jobs):
        create_job(jobs, "one", "x")
        mark_held(jobs, "one", "blocked on credentials")
        text = open(list_jobs(jobs)[0].path).read()
        assert "STATUS: held" in text
        assert "blocked on credentials" in text

    def test_mark_done_moves_file(self, jobs):
        create_job(jobs, "one", "x")
        mark_done(jobs, "one")
        assert not list_jobs(jobs)
        archived = os.path.join(jobs, "done")
        assert os.path.isdir(archived)
        files = [f for f in os.listdir(archived) if f.endswith("-one.md")]
        assert len(files) == 1
        assert "STATUS: done" in open(os.path.join(archived, files[0])).read()


# ---------------------------------------------------------------------------
# worker_limit config in liveness.toml
# ---------------------------------------------------------------------------

class TestWorkerLimitConfig:
    def test_reads_value_from_toml(self, tmp_path):
        cfg = os.path.join(tmp_path, "liveness.toml")
        open(cfg, "w").write("[queue]\nworker_limit = 5\n")
        assert load_worker_limit(cfg) == 5

    def test_default_3_when_missing(self, tmp_path):
        assert load_worker_limit(tmp_path / "nope.toml") == 3

    def test_default_3_when_section_absent(self, tmp_path):
        cfg = os.path.join(tmp_path, "liveness.toml")
        open(cfg, "w").write("[tiers]\nwarn_after_s = 900\n")
        assert load_worker_limit(cfg) == 3
