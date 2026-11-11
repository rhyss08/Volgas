"""Where Volgas keeps its files, and safe loading/saving of its settings.

Settings used to live in volgas_settings.json next to Volgas.exe, written
straight over the old file with every error silently ignored. That had
three problems: a crash mid-write left a damaged file that the next start
quietly replaced with defaults; a Volgas.exe in a protected folder (e.g.
Program Files) couldn't save at all; and nothing told anyone either had
happened. Now:

- files live in the user's own data folder (data_dir below);
- saves are atomic: written to a temporary file, then swapped in, so the
  file on disk is always either the old or the new version, never half;
- a damaged file is set aside (not deleted) and the problem is logged;
- the old file next to the exe is copied over once, then left in place so
  an older Volgas version can still read it after a downgrade.
"""
import json
import logging
import os
import sys
import tempfile
import time

log = logging.getLogger(__name__)

SETTINGS_FILE_NAME = "volgas_settings.json"
APP_DIR_NAME = "Volgas"


def data_dir():
    """Per-user folder for settings and logs. VOLGAS_DATA_DIR overrides it
    (used by the tests and the hardware simulator)."""
    override = os.environ.get("VOLGAS_DATA_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_DIR_NAME)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), APP_DIR_NAME)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_DIR_NAME.lower())


def log_dir():
    return os.path.join(data_dir(), "logs")


def settings_path():
    return os.path.join(data_dir(), SETTINGS_FILE_NAME)


def legacy_settings_path():
    """Where v0.2.4 and older kept the file: next to Volgas.exe, or next to
    volgas.py when run from source."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, SETTINGS_FILE_NAME)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings file does not contain a JSON object")
    return data


def _set_aside(path):
    """Keep a damaged file for inspection instead of overwriting it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = f"{path}.corrupt-{stamp}"
    try:
        os.replace(path, target)
        log.warning("settings file was unreadable; kept a copy as %s and started from defaults", target)
    except OSError:
        log.warning("settings file %s was unreadable and could not be set aside", path, exc_info=True)


def load_settings():
    """The saved settings dict, or {} if there are none yet. Never raises."""
    path = settings_path()
    if os.path.exists(path):
        try:
            return _read_json(path)
        except (OSError, ValueError):
            log.warning("could not read settings from %s", path, exc_info=True)
            _set_aside(path)
            return {}

    legacy = legacy_settings_path()
    if os.path.normcase(os.path.abspath(legacy)) != os.path.normcase(os.path.abspath(path)) and os.path.exists(legacy):
        try:
            data = _read_json(legacy)
        except (OSError, ValueError):
            log.warning("old settings file %s was unreadable; ignoring it", legacy, exc_info=True)
            return {}
        if save_settings(data):
            log.info("moved settings from %s to %s", legacy, path)
        return data
    return {}


def save_settings(settings):
    """Atomically write settings. Returns True on success; failures are
    logged rather than raised, because losing a preference must never
    take down the app."""
    path = settings_path()
    folder = os.path.dirname(path)
    tmp = None
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".volgas_settings.", suffix=".tmp", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        log.error("could not save settings to %s", path, exc_info=True)
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False
