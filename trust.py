"""trust.py — pre-seed Claude Code trust state before launching a session.

Claude Code pauses on launch in an untrusted directory with:

    Do you trust the files in this folder?

There is **no CLI flag and no environment variable** that suppresses it
(verified empirically against 2.1.285: --dangerously-skip-permissions,
--permission-mode bypassPermissions, CLAUDE_BG_WORKSPACE_TRUSTED,
CLAUDE_CODE_RELAUNCH_HOME_TRUST all still show the dialog; `claude -p`
skips the dialog but never records acceptance). Trust is persisted *state*,
and Claude Code's own error message documents the supported non-interactive
path:

    ...or set projects["<path>"].hasTrustDialogAccepted: true
        in ~/.claude.json

Two independent gates read the same project entry, and both must be set or
the launch still hangs:

  hasTrustDialogAccepted               -> "Do you trust this folder?" dialog
  hasClaudeMdExternalIncludesApproved  -> "Allow external CLAUDE.md file
                                          imports?" dialog

Resolution rules that make a naive implementation fail:

  * Keys are **realpath**-normalised (macOS /tmp -> /private/tmp, no
    trailing slash). Writing the unresolved path silently does nothing.
  * The dialog gate cascades to parent directories **but stops at the git
    root**; the settings gate is an exact match with no inheritance. Since
    `git init`'d projects are the normal case, the exact directory must be
    trusted — which is fine, because the bridge launches at a known cwd.
  * $HOME trust is never persisted by Claude Code (it resets to false).

This module therefore writes exactly the launch directory, merging into the
existing config rather than replacing it — Claude Code rewrites that file
continuously and truncating it destroys credentials and all other projects.
"""
import json
import os
import tempfile

TRUST_KEYS = ("hasTrustDialogAccepted", "hasClaudeMdExternalIncludesApproved")

#: How many read-modify-write rounds before giving up on a file that keeps
#: changing under us (see `preseed_trust`).
TRUST_WRITE_ATTEMPTS = 5


def claude_config_path():
    """Resolve the Claude Code config file, honouring CLAUDE_CONFIG_DIR."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return os.path.join(os.path.expanduser(config_dir), ".claude.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def normalized_key(path):
    """The exact project key Claude Code uses for `path`."""
    return os.path.realpath(os.path.expanduser(path)).replace(os.sep, "/")


def _stat_ns(path):
    """Modification time in ns, or None when the file does not exist."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def preseed_trust(cwd, config_path=None):
    """Mark `cwd` trusted in the Claude Code config, before launching.

    Returns a dict describing what happened::

        {"key": "<resolved path>", "changed": ["hasTrustDialogAccepted", ...]}

    or ``{"error": ..., "message": ...}`` if the config could not be read.
    On error the config file is left exactly as it was.

    Claude Code rewrites this same file continuously, so this is a
    read-modify-write against a moving target. The atomic replace stops a
    torn file but not a *lost update*: anything Claude wrote between the read
    and the replace would be dropped. The mtime is therefore re-checked
    immediately before the replace, and a change re-reads and re-applies
    instead of overwriting (2026-11-03 review, finding 10).
    """
    config_path = config_path or claude_config_path()
    key = normalized_key(cwd)

    for _ in range(TRUST_WRITE_ATTEMPTS):
        seen = _stat_ns(config_path)
        try:
            with open(config_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (json.JSONDecodeError, OSError) as e:
            # Never clobber a config we cannot parse — it holds credentials.
            return {"error": "config_unreadable",
                    "message": f"Cannot read {config_path}: {e}", "key": key}

        if not isinstance(data, dict):
            return {"error": "config_malformed",
                    "message": f"{config_path} is not a JSON object", "key": key}

        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            return {"error": "config_malformed",
                    "message": f"{config_path}: 'projects' is not an object", "key": key}

        entry = projects.setdefault(key, {})
        if not isinstance(entry, dict):
            return {"error": "config_malformed",
                    "message": f"{config_path}: project entry for {key} is not an object",
                    "key": key}

        changed = [k for k in TRUST_KEYS if entry.get(k) is not True]
        if not changed:
            return {"key": key, "changed": []}

        for k in changed:
            entry[k] = True

        if _stat_ns(config_path) != seen:
            continue                     # Claude wrote mid-flight: re-read
        _atomic_write_json(config_path, data)
        return {"key": key, "changed": changed}

    return {"error": "config_busy",
            "message": f"{config_path} kept changing under this write "
                       f"({TRUST_WRITE_ATTEMPTS} attempts); retry on the next launch",
            "key": key}


def _atomic_write_json(path, data):
    """Write JSON via a temp file + os.replace, preserving file mode.

    Atomic so an interrupted write cannot leave a truncated config; mode-
    preserving because ~/.claude.json holds credentials (typically 0600).
    """
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".claude.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
