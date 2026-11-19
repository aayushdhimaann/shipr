"""Per-user locations (global config, shipr's known_hosts)."""

from pathlib import Path

import click


def app_dir() -> Path:
    """~/.config/shipr on Linux, ~/Library/Application Support/shipr on macOS, %APPDATA%\\shipr."""
    return Path(click.get_app_dir("shipr"))
