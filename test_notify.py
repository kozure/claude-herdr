"""Tests for notify.py — 3-layer completion notify (Task 3, plan groups a–f).

All transports are fakes. No real push, no real submission, no real files
beyond tmp_path. Groups:
  (a) push success  -> marker says sent, no fallback
  (b) push failure  -> background fallback (argv contains --background),
                       marker says fallback + TASK_ID present (M1)
  (c) all fail      -> marker UNSENT with reason + gap flag; returns failure
                       status; never raises past caller
  (d) marker format: run_id/verdict/grade/report_path/digest always;
                       fallback TASK_ID only when layer 2 fired
  (e) no timeout/task_timeout kwargs ever reach a transport call
  (f) source enforcement: every chat-transport line in notify.py carries
      --background; the module contains no timer arguments at all
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notify
from notify import notify_completion


# ---------------------------------------------------------------------------
# Fake transports
# ---------------------------------------------------------------------------

class FakeTransports:
    """Records every transport call; behaviour configurable per test."""

    def __init__(self, push_ok=True, background_ok=True, task_id="task-abc-123",
                 discovery_ok=True):
        self.push_calls = []          # (argv, kwargs)
        self.background_calls = []    # (argv, kwargs)
        self.discovery_calls = 0
        self.push_ok = push_ok
        self.background_ok = background_ok
        self.discovery_ok = discovery_ok
        self.task_id = task_id

    # -- layer 1 ------------------------------------------------------------
    def discover_target(self):
        self.discovery_calls += 1
        if not self.discovery_ok:
            raise RuntimeError("discovery unavailable")
        return {"channel": "console", "target_user": "default",
                "target_session": "sess-42"}

    def push(self, argv, **kwargs):
        self.push_calls.append((argv, kwargs))
        if not self.push_ok:
            raise RuntimeError("channels send failed")
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    # -- layer 2 ------------------------------------------------------------
    def submit_background(self, argv, **kwargs):
        self.background_calls.append((argv, kwargs))
        if not self.background_ok:
            raise RuntimeError("background submit failed")
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"[TASK_ID: {self.task_id}]", stderr="")
        # pragma: no cover


@pytest.fixture
def tmp_notice(tmp_path):
    return tmp_path / "cases" / "run-test-1"


BASE = dict(run_id="run-test-1", verdict="PASS", grade=89,
            report_path="/tmp/report.md", digest="abcd1234")


# ---------------------------------------------------------------------------
# (a) push success
# ---------------------------------------------------------------------------

class TestPushSuccess:
    def test_status_sent_no_fallback(self, tmp_notice):
        t = FakeTransports(push_ok=True)
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert status == "sent"
        assert len(t.push_calls) >= 1
        assert t.background_calls == []

    def test_marker_says_sent(self, tmp_notice):
        t = FakeTransports(push_ok=True)
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        marker = (tmp_notice / "completion_notice.md").read_text()
        assert "sent" in marker

    def test_push_uses_discovered_target(self, tmp_notice):
        t = FakeTransports(push_ok=True)
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        argv = t.push_calls[0][0]
        assert "sess-42" in " ".join(argv)
        assert t.discovery_calls == 1

    def test_one_retry_max_on_push(self, tmp_notice):
        """Push failure retries once (cheap, one-way), then falls through."""
        t = FakeTransports(push_ok=False, background_ok=False)
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert len(t.push_calls) == 2  # initial + one retry, no more


# ---------------------------------------------------------------------------
# (b) push failure -> background fallback
# ---------------------------------------------------------------------------

class TestBackgroundFallback:
    def test_argv_contains_background_flag(self, tmp_notice):
        t = FakeTransports(push_ok=False)
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert status == "fallback"
        assert len(t.background_calls) == 1
        argv = t.background_calls[0][0]
        assert "--background" in argv
        # N1: the foreground primitive is prohibited
        joined = " ".join(argv)
        assert "agents" in joined and "chat" in joined

    def test_marker_says_fallback_with_task_id(self, tmp_notice):
        t = FakeTransports(push_ok=False, task_id="task-xyz-9")
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        marker = (tmp_notice / "completion_notice.md").read_text()
        assert "fallback" in marker
        assert "task-xyz-9" in marker  # M1: TASK_ID recorded

    def test_discovery_failure_goes_to_fallback(self, tmp_notice):
        t = FakeTransports(discovery_ok=False)
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert status == "fallback"
        assert len(t.background_calls) == 1


# ---------------------------------------------------------------------------
# (c) everything fails
# ---------------------------------------------------------------------------

class TestAllFail:
    def test_returns_unsent_and_never_raises(self, tmp_notice):
        t = FakeTransports(push_ok=False, background_ok=False)
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert status.startswith("UNSENT")

    def test_marker_unsent_with_reason_and_gap_flag(self, tmp_notice):
        t = FakeTransports(push_ok=False, background_ok=False)
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        marker = (tmp_notice / "completion_notice.md").read_text()
        assert "UNSENT" in marker
        assert "reason" in marker.lower()
        assert "notify may not have landed" in marker

    def test_no_task_id_when_layer2_never_fired(self, tmp_notice):
        t = FakeTransports(push_ok=False, background_ok=False)
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        marker = (tmp_notice / "completion_notice.md").read_text()
        assert "fallback_task_id" not in marker  # M1: absent when layer 2 didn't fire


# ---------------------------------------------------------------------------
# (d) marker format
# ---------------------------------------------------------------------------

class TestMarkerFormat:
    def test_core_fields_always_present(self, tmp_notice):
        t = FakeTransports(push_ok=True)
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        marker = (tmp_notice / "completion_notice.md").read_text()
        for field in ("run-test-1", "PASS", "89", "/tmp/report.md", "abcd1234"):
            assert field in marker

    def test_task_id_present_iff_layer2_fired(self, tmp_notice):
        t_ok = FakeTransports(push_ok=True)
        notify_completion(notice_dir=tmp_notice, transport=t_ok, **BASE)
        m1 = (tmp_notice / "completion_notice.md").read_text()
        t_fb = FakeTransports(push_ok=False)
        notify_completion(notice_dir=tmp_notice, transport=t_fb, **BASE)
        m2 = (tmp_notice / "completion_notice.md").read_text()
        assert "fallback_task_id" not in m1
        assert "fallback_task_id" in m2

    def test_marker_survives_even_if_marker_dir_missing(self, tmp_notice):
        t = FakeTransports(push_ok=False, background_ok=False)
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert (tmp_notice / "completion_notice.md").exists()
        assert status.startswith("UNSENT")


# ---------------------------------------------------------------------------
# Regression: real `chats list` CLI shape (top-level JSON array, real field
# names user_id / updated_at). The FakeTransports class fakes discover_target
# entirely, so this shape was never pinned — the live push could never fire.
# ---------------------------------------------------------------------------

class TestRealCliShape:
    def test_parse_top_level_array(self):
        raw = ('[{"id": "sess-a", "channel": "console", "user_id": "default", '
               '"updated_at": "2026-08-30T18:00:00Z"}]')
        sessions = notify._parse_sessions(raw)
        assert len(sessions) == 1
        best = notify._pick_best(sessions)
        assert best["id"] == "sess-a"
        assert best["user_id"] == "default"

    def test_parse_wrapped_dict_still_works(self):
        raw = '{"sessions": [{"id": "sess-b", "channel": "console", "user": "alice", "updatedAt": "2026-08-30T17:00:00Z"}]}'
        sessions = notify._parse_sessions(raw)
        assert len(sessions) == 1
        best = notify._pick_best(sessions)
        assert best["id"] == "sess-b"

    def test_pick_best_prefers_most_recent(self):
        raw = ('[{"id": "older", "channel": "console", "updated_at": "2026-08-30T18:00:00Z"}, '
               '{"id": "newer", "channel": "console", "updated_at": "2026-08-30T19:00:00Z"}]')
        best = notify._pick_best(notify._parse_sessions(raw))
        assert best["id"] == "newer"

    def test_discover_target_against_real_cli_shape(self, tmp_notice, monkeypatch):
        """Full path: SubprocessTransport.discover_target on real-shape output."""
        raw = ('[{"id": "sess-live", "channel": "console", "user_id": "default", '
               '"updated_at": "2026-08-30T19:30:00Z"}]')
        fake_proc = subprocess.CompletedProcess([], 0, stdout=raw, stderr="")
        monkeypatch.setattr(notify.subprocess, "run",
                            lambda argv, **kw: fake_proc)
        t = FakeTransports(push_ok=True)
        real_discover = notify.SubprocessTransport().discover_target
        t.discover_target = real_discover
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert status == "sent"
        argv = t.push_calls[0][0]
        joined = " ".join(argv)
        assert "--agent-id" in joined        # push now carries required agent id
        assert "sess-live" in joined         # session from real-shape discovery


# ---------------------------------------------------------------------------
# (e) no timer arguments anywhere
# ---------------------------------------------------------------------------

class TestNoTimers:
    def test_no_timeout_kwargs_reach_transports(self, tmp_notice):
        t = FakeTransports(push_ok=False)  # exercises push + fallback
        notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        for calls in (t.push_calls, t.background_calls):
            for argv, kwargs in calls:
                assert "timeout" not in kwargs
                assert "task_timeout" not in kwargs

    def test_module_has_no_task_timeout_token(self):
        src = open(notify.__file__).read()
        assert "task_timeout" not in src
        assert "timeout=" not in src


# ---------------------------------------------------------------------------
# (f) source-level backgroundness enforcement (N1)
# ---------------------------------------------------------------------------

class TestSourceEnforcement:
    def test_every_chat_transport_line_has_background_flag(self):
        src = open(notify.__file__).read().splitlines()
        chat_lines = [ln for ln in src if "agents" in ln and "chat" in ln]
        assert chat_lines, "module must contain its chat transport argv"
        for ln in chat_lines:
            assert "--background" in ln, f"foreground chat transport: {ln}"


# ---------------------------------------------------------------------------
# Task 6 / D4: failure injection — HERDR_NOTIFY_DEAD_SOCKET=1 forces the
# primary push transport to fail so the fallback path is exercisable live.
# ---------------------------------------------------------------------------

class TestDeadSocketInjection:
    def test_env_var_forces_push_failure(self, tmp_notice, monkeypatch):
        monkeypatch.setenv("HERDR_NOTIFY_DEAD_SOCKET", "1")
        t = FakeTransports(push_ok=True)  # transport healthy; env must win
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert status == "fallback"
        assert len(t.push_calls) == 0          # injection short-circuits before transport
        assert t.background_calls == 1 or len(t.background_calls) == 1

    def test_env_var_absent_pushes_normally(self, tmp_notice, monkeypatch):
        monkeypatch.delenv("HERDR_NOTIFY_DEAD_SOCKET", raising=False)
        t = FakeTransports(push_ok=True)
        status = notify_completion(notice_dir=tmp_notice, transport=t, **BASE)
        assert status == "sent"
