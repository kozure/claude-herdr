"""Tests for com.alfred.bridge-timer.plist — the launchd unit (parent 5.0).

The plist is parsed with `plistlib`, never read by eye: the unattended blast
radius (no `--allow-spend` on the 60 s tick) is bounded by construction, and
this file is what proves the construction. Path built from `__file__`, so the
test passes from `claude_bridge/` and from the repo root alike.

The shipped plist is a template: every home-relative path is written against
the `/Users/USERNAME` placeholder so the repo carries no local account name.
`_load()` renders the placeholder to the real home directory before anything
is asserted, so the path assertions still prove the shape of a real install.
Substitute the placeholder in your own copy when installing the launchd unit.
"""
import os
import plistlib

HERE = os.path.dirname(os.path.abspath(__file__))
PLIST = os.path.join(HERE, "com.alfred.bridge-timer.plist")
DATA_DIR = os.path.expanduser("~/.local/share/claude_bridge")  # ledger._default_db_path()'s dir
HOME_PLACEHOLDER = "/Users/USERNAME"
REAL_HOME = os.path.expanduser("~")


def _render(value):
    """Replace the shipped placeholder with the local home directory."""
    if isinstance(value, str):
        return value.replace(HOME_PLACEHOLDER, REAL_HOME)
    if isinstance(value, list):
        return [_render(v) for v in value]
    if isinstance(value, dict):
        return {k: _render(v) for k, v in value.items()}
    return value


def _load():
    with open(PLIST, "rb") as fh:
        return _render(plistlib.load(fh))


def test_label():
    assert _load()["Label"] == "com.alfred.bridge-timer"


def test_start_interval_is_60():
    assert _load()["StartInterval"] == 60


def test_run_at_load_is_true():
    assert _load()["RunAtLoad"] is True


def test_one_shot_tick_has_no_keepalive():
    # `--tick` is a one-shot (spec §Design): KeepAlive would make launchd respawn it.
    assert "KeepAlive" not in _load()


def test_log_paths_under_bridge_data_dir():
    plist = _load()
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = plist[key]
        assert os.path.isabs(path), key
        assert os.path.dirname(path) == DATA_DIR, key


def test_program_arguments_absolute_and_end_in_timer_tick():
    args = _load()["ProgramArguments"]
    assert os.path.isabs(args[0])
    assert os.path.isabs(args[1])
    assert args[1] == os.path.join(HERE, "timer.py")
    assert args[-1] == "--tick"
    assert args[2:] == ["--tick"]


def test_no_allow_spend_in_program_arguments():
    args = _load()["ProgramArguments"]
    assert "--allow-spend" not in args
    assert not any("allow-spend" in a for a in args)


def test_working_directory_is_module_dir():
    assert _load()["WorkingDirectory"] == HERE
