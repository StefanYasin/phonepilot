"""Filesystem layout for PhonePilot state.

Everything lives under ``PHONEPILOT_HOME`` (default ``~/.phonepilot``) so a
test or a second fleet never touches the real registry:

    ~/.phonepilot/phonepilot.db   SQLite registry, runs, schedules
    ~/.phonepilot/templates/      user editable copies of shipped templates
    ~/.phonepilot/media/queue/    photos waiting to be posted
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Return the PhonePilot home directory."""
    raw = os.environ.get("PHONEPILOT_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".phonepilot"


def ensure_home() -> Path:
    """Create the home directory tree and return it."""
    root = home()
    for child in ("", "templates", "templates/media/queue"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    """Return the SQLite file path, creating its parent directory."""
    raw = os.environ.get("PHONEPILOT_DB")
    path = Path(raw).expanduser() if raw else home() / "phonepilot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def templates_dir() -> Path:
    """Directory where ``phonepilot init`` copies editable templates."""
    return home() / "templates"


def shipped_templates_dir() -> Path:
    """Templates bundled with the installed package."""
    return Path(__file__).resolve().parent / "templates"
