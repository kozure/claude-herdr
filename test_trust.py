"""Tests for trust.py — pre-seeding Claude Code trust state before launch.

Why this exists: Claude Code pauses on launch with "Do you trust this folder?"
There is no CLI flag or env var to skip it (verified against 2.1.285). The only
supported non-interactive path is to pre-seed the trust record in ~/.claude.json
before launching (Anthropic's own error message documents the edit).

Two independent gates use the same project entry:
  - hasTrustDialogAccepted            -> the trust dialog
  - hasClaudeMdExternalIncludesApproved -> the "external CLAUDE.md imports" dialog
Both must be set or the launch still hangs.
"""
import json
import os

import pytest

from trust import (
    TRUST_KEYS,
    claude_config_path,
    normalized_key,
    preseed_trust,
)

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    """Path to a config file that does not exist yet."""
    return str(tmp_path / "claude.json")


def read_cfg(path):
    with open(path) as f:
        return json.load(f)


def write_cfg(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------

class TestConfigPath:
    def test_default_is_home_claude_json(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", "/Users/someone")
        assert claude_config_path() == "/Users/someone/.claude.json"

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/claude-cfg")
        assert claude_config_path() == "/opt/claude-cfg/.claude.json"

    def test_env_override_expands_user(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/someone")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/cfgdir")
        assert claude_config_path() == "/Users/someone/cfgdir/.claude.json"


# ---------------------------------------------------------------------------
# Key normalisation
# ---------------------------------------------------------------------------

class TestNormalizedKey:
    def test_absolute_path_kept(self):
        assert normalized_key(f"{HOME}/DEV") == f"{HOME}/DEV"

    def test_trailing_slash_stripped(self):
        assert normalized_key(f"{HOME}/DEV/") == f"{HOME}/DEV"

    def test_symlinks_resolved(self, tmp_path):
        """macOS /tmp -> /private/tmp; the key must be the resolved path."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert normalized_key(str(link)) == normalized_key(str(real))
        assert normalized_key(str(link)) == os.path.realpath(str(link)).replace(os.sep, "/")

    def test_relative_path_resolved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sub").mkdir()
        assert normalized_key("sub") == normalized_key(str(tmp_path / "sub"))


# ---------------------------------------------------------------------------
# Pre-seeding
# ---------------------------------------------------------------------------

class TestPreseedTrust:
    def test_creates_config_when_absent(self, cfg, tmp_path):
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        result = preseed_trust(cwd, config_path=cfg)
        assert os.path.exists(cfg)
        data = read_cfg(cfg)
        assert data["projects"][normalized_key(cwd)]["hasTrustDialogAccepted"] is True
        assert result["key"] == normalized_key(cwd)

    def test_sets_both_gates(self, cfg, tmp_path):
        """Trust alone still hangs on the external-imports dialog."""
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        preseed_trust(cwd, config_path=cfg)
        entry = read_cfg(cfg)["projects"][normalized_key(cwd)]
        for key in TRUST_KEYS:
            assert entry[key] is True

    def test_returns_changed_keys(self, cfg, tmp_path):
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        result = preseed_trust(cwd, config_path=cfg)
        assert sorted(result["changed"]) == sorted(TRUST_KEYS)

    def test_preserves_existing_config(self, cfg, tmp_path):
        """Never replace the file — Claude rewrites it continuously."""
        write_cfg(cfg, {
            "numStartups": 42,
            "projects": {f"{HOME}/DEV": {"hasTrustDialogAccepted": True,
                                                 "allowedTools": ["Read"]}},
            "oauthAccount": {"emailAddress": "x@example.com"},
        })
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        preseed_trust(cwd, config_path=cfg)
        data = read_cfg(cfg)
        assert data["numStartups"] == 42
        assert data["oauthAccount"] == {"emailAddress": "x@example.com"}
        assert data["projects"][f"{HOME}/DEV"]["allowedTools"] == ["Read"]
        assert len(data["projects"]) == 2

    def test_preserves_other_keys_in_same_entry(self, cfg, tmp_path):
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        key = normalized_key(cwd)
        write_cfg(cfg, {"projects": {key: {"allowedTools": ["Bash"], "mcpServers": {}}}})
        preseed_trust(cwd, config_path=cfg)
        entry = read_cfg(cfg)["projects"][key]
        assert entry["allowedTools"] == ["Bash"]
        assert entry["mcpServers"] == {}
        assert entry["hasTrustDialogAccepted"] is True

    def test_idempotent_does_not_rewrite(self, cfg, tmp_path):
        """A second call with nothing to change must not touch the file."""
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        preseed_trust(cwd, config_path=cfg)
        before = os.stat(cfg).st_mtime_ns
        result = preseed_trust(cwd, config_path=cfg)
        assert os.stat(cfg).st_mtime_ns == before
        assert result["changed"] == []

    def test_symlinked_cwd_writes_resolved_key(self, cfg, tmp_path):
        real = tmp_path / "real-proj"
        real.mkdir()
        link = tmp_path / "link-proj"
        link.symlink_to(real)
        preseed_trust(str(link), config_path=cfg)
        projects = read_cfg(cfg)["projects"]
        assert normalized_key(str(real)) in projects
        assert str(link) not in projects

    def test_no_trailing_slash_in_key(self, cfg, tmp_path):
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        preseed_trust(cwd + "/", config_path=cfg)
        key = normalized_key(cwd)
        assert key.endswith("proj")
        assert key in read_cfg(cfg)["projects"]

    def test_corrupt_config_is_not_clobbered(self, cfg, tmp_path):
        """An unreadable config must fail loudly, never be silently replaced."""
        with open(cfg, "w") as f:
            f.write("{ this is not json")
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        result = preseed_trust(cwd, config_path=cfg)
        assert "error" in result
        with open(cfg) as f:
            assert f.read() == "{ this is not json"

    def test_no_temp_files_left_behind(self, cfg, tmp_path):
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        preseed_trust(cwd, config_path=cfg)
        leftovers = [n for n in os.listdir(os.path.dirname(cfg))
                     if n.endswith(".tmp")]
        assert leftovers == []

    def test_result_is_json_serialisable(self, cfg, tmp_path):
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        result = preseed_trust(cwd, config_path=cfg)
        json.dumps(result)

    def test_preserves_file_permissions(self, cfg, tmp_path):
        """~/.claude.json holds credentials — mode must survive a rewrite."""
        cwd = str(tmp_path / "proj")
        os.makedirs(cwd)
        write_cfg(cfg, {"projects": {}})
        os.chmod(cfg, 0o600)
        preseed_trust(cwd, config_path=cfg)
        assert oct(os.stat(cfg).st_mode)[-3:] == "600"


# ---------------------------------------------------------------------------
# 2026-11-03 review, finding 10: the read-modify-write loses concurrent writes
# ---------------------------------------------------------------------------

def test_a_concurrent_write_between_read_and_replace_is_not_dropped(tmp_path, monkeypatch):
    """Claude Code rewrites ~/.claude.json continuously.

    The atomic replace prevents a torn file but not a lost update. If the file
    changes between the read and the replace, the pass must re-read and re-apply
    instead of overwriting whatever landed in between.
    """
    import trust

    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": {}}))

    stats = {"n": 0}
    real_stat = trust._stat_ns

    def racing_stat(path):
        stats["n"] += 1
        if stats["n"] == 2:            # the stat taken right after the read
            live = json.loads(config.read_text())
            live["written_by_claude_code"] = {"mid_flight": True}
            config.write_text(json.dumps(live))
        return real_stat(path)

    monkeypatch.setattr(trust, "_stat_ns", racing_stat)
    result = preseed_trust(f"{HOME}/DEV/raced", config_path=str(config))

    written = json.loads(config.read_text())
    assert written["written_by_claude_code"] == {"mid_flight": True}
    assert written["projects"][normalized_key(f"{HOME}/DEV/raced")]["hasTrustDialogAccepted"] is True
    assert set(result["changed"]) == set(TRUST_KEYS)


def test_a_file_that_never_settles_reports_config_busy(tmp_path, monkeypatch):
    """Every stat differs, so every attempt sees a mid-flight write."""
    import trust

    config = tmp_path / ".claude.json"
    original = json.dumps({"projects": {}})
    config.write_text(original)
    calls = {"n": 0}

    def always_changing(path):
        calls["n"] += 1
        return calls["n"]

    monkeypatch.setattr(trust, "_stat_ns", always_changing)
    result = preseed_trust(f"{HOME}/DEV/never", config_path=str(config))

    assert result["error"] == "config_busy"
    assert calls["n"] == 2 * trust.TRUST_WRITE_ATTEMPTS     # stat before + after each read
    assert config.read_text() == original                   # nothing clobbered
