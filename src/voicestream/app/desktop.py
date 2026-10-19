"""Opening things in the desktop: Finder for a folder or file, System Settings for a permission.

Both take a *name* from a fixed list, never a path or URL from the request: the local API must
not become a way to open arbitrary files or URL schemes on your Mac.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

SETTINGS_PANES = {
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "screen_audio": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
    "login_items": "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
}
REVEAL_TARGETS = ("data_dir", "config", "log", "meeting")


class DesktopError(RuntimeError):
    pass


def _launch(argv: list[str]) -> None:
    """Start ``argv`` detached (no shell, no output); separate so tests can record it."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def reveal_command(path: Path) -> list[str]:
    if sys.platform == "darwin":
        # a folder opens as a window; a file is selected in its folder. So is a folder with an extension:
        # `open X.app` (or .pkg, .bundle…) would start it rather than show it, and data_dir is a setting
        is_plain_folder = path.is_dir() and not path.suffix
        return ["open", str(path)] if is_plain_folder else ["open", "-R", str(path)]
    if sys.platform.startswith("win"):
        return ["explorer", str(path)] if path.is_dir() else ["explorer", f"/select,{path}"]
    return ["xdg-open", str(path if path.is_dir() else path.parent)]


def reveal(path: Path) -> None:
    if not path.exists():
        raise DesktopError(f"{path.name or path} doesn't exist yet")
    try:
        _launch(reveal_command(path))
    except OSError as exc:
        raise DesktopError(f"could not open the file browser: {exc}") from None


def open_settings(pane: str) -> None:
    url = SETTINGS_PANES.get(pane)
    if url is None:
        raise DesktopError(f"unknown settings pane {pane!r}")
    if sys.platform != "darwin":
        raise DesktopError("System Settings panes exist on macOS only")
    try:
        _launch(["open", url])
    except OSError as exc:
        raise DesktopError(f"could not open System Settings: {exc}") from None
