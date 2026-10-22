"""The Spotlight launcher: a tiny "Voicestream – New Meeting" app in ~/Applications.

⌘Space, "voicestream", Return: Spotlight finds apps by name, so a second, one-purpose app is how a
meeting starts from the keyboard without the window. The launcher holds no code of ours: its
executable is a shell script that asks macOS to open the ``voicestream://meeting/start?key=…`` link
(:meth:`voicestream.app.host.AppHost.launcher_url`) with Voicestream, in the background (``open -g``:
the app you were in keeps the focus), which starts the meeting whether Voicestream was running or
not. The key is what lets that link start a recording (any app or web page can open a link): it is
written into the script only, which is readable by you only.

The native shell keeps the launcher in step with the preference (``prefs.spotlight_launcher``): it
is written while the setting is on, rewritten when the key, the app's place or the language
changes, and removed when the setting is turned off. Everything here is a function of the folder
it is given (tests use a temporary one); only :func:`applications_dir` names the real one. The
bundle is written by the app itself, so it carries no quarantine and Gatekeeper has nothing to
ask; it is not signed, and needs not be: its executable is a script ``/bin/sh`` runs.

A launcher is recognised by its Info.plist (:data:`LAUNCHER_ID` and :data:`MARK`), never by its
name alone: a folder of the same name that isn't ours is left as it is.
"""

from __future__ import annotations

import contextlib
import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_ID = "io.github.bybysker.voicestream"
"""The main app's bundle id (packaging/mac/bundle_info.py ``BUNDLE_ID``): the script's fallback when
the app moved since the launcher was written."""
LAUNCHER_ID = f"{APP_ID}.new-meeting"
MARK = "VoicestreamLauncher"
"""An Info.plist key only our launcher has (its value: :data:`FORMAT`)."""
FORMAT = 1
NAMES = {"en": "Voicestream – New Meeting", "fr": "Voicestream – Nouvelle réunion"}
"""The launcher's name, in Spotlight and in Finder: "voicestream" finds it in both languages."""
EXECUTABLE = "new-meeting"
ICON = "AppIcon.icns"
SCRIPT_MODE = 0o700
"""The script holds the link's key: readable (and runnable) by you only."""
MIN_MACOS = "14.2"  # as the main app (bundle_info.MIN_MACOS)
PLUTIL = "/usr/bin/plutil"
LINK_PREFIX = "voicestream://meeting/start?key="


def applications_dir() -> Path:
    """``~/Applications``: Spotlight and Launchpad list the apps in it, and it needs no administrator."""
    return Path("~/Applications").expanduser()


@dataclass(frozen=True, slots=True)
class Launcher:
    """What the launcher should be. ``url`` is the keyed link: never log it."""

    url: str
    lang: str = "en"
    app: Path | None = None
    """The running Voicestream.app, opened by its path (the right copy when there are several)."""
    icon: Path | None = None
    version: str = "0"

    @property
    def name(self) -> str:
        return NAMES.get(self.lang, NAMES["en"])

    def __repr__(self) -> str:  # the link carries the key
        return f"Launcher(name={self.name!r}, app={self.app!r})"


def script(url: str, app: Path | None = None) -> str:
    """The launcher's executable: open the link with the Voicestream at ``app`` in the background, or, if it
    isn't there any more (moved, or a new copy installed), with whichever Voicestream macOS knows by its id."""
    link = shlex.quote(url)
    by_id = f"exec /usr/bin/open -g -b {APP_ID} {link}"
    lines = [
        "#!/bin/sh",
        f"# {NAMES['en']}: starts a meeting in Voicestream, as its Start button does.",
        "# Voicestream wrote this launcher (Settings → General → Starting meetings) and removes it when you",
        "# turn it off there. The link carries a key: only this launcher may start a recording with it.",
    ]
    if app is not None:
        # a Mac shell runs it: the path in / form whatever system wrote it (the tests run on Windows too)
        lines.append(f"/usr/bin/open -g -a {shlex.quote(app.as_posix())} {link} 2>/dev/null || {by_id}")
    else:
        lines.append(by_id)
    return "\n".join(lines) + "\n"


def info_plist(name: str, version: str, *, icon: bool) -> dict[str, Any]:
    plist: dict[str, Any] = {
        "CFBundleIdentifier": LAUNCHER_ID,
        "CFBundleName": "New Meeting",  # the short name (15 characters at most); Finder and Spotlight show the folder's
        "CFBundleDisplayName": name,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSUIElement": True,  # no Dock icon, no menu bar of its own: it opens a link and is gone
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSHumanReadableCopyright": "Voicestream — MIT licence",
        MARK: FORMAT,
    }
    if icon:
        plist["CFBundleIconFile"] = ICON
    return plist


def app_icon(app: Path | None) -> Path | None:
    """The main app's icon file (its Info.plist's ``CFBundleIconFile``, in ``Contents/Resources``)."""
    if app is None:
        return None
    contents = app / "Contents"
    try:
        with (contents / "Info.plist").open("rb") as fh:
            name = str(plistlib.load(fh).get("CFBundleIconFile") or "")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not name:
        return None
    icon = contents / "Resources" / (name if name.endswith(".icns") else f"{name}.icns")
    return icon if icon.is_file() else None


def _files(launcher: Launcher) -> dict[str, tuple[bytes, int]]:
    """``Contents/…`` path → (bytes, mode) of every file the launcher is made of."""
    icon = None
    if launcher.icon is not None:
        with contextlib.suppress(OSError):
            icon = launcher.icon.read_bytes()
    files = {
        "Contents/Info.plist": (plistlib.dumps(info_plist(launcher.name, launcher.version, icon=icon is not None)), 0o644),
        "Contents/PkgInfo": (b"APPL????", 0o644),
        f"Contents/MacOS/{EXECUTABLE}": (script(launcher.url, launcher.app).encode("utf-8"), SCRIPT_MODE),
    }
    if icon is not None:
        files[f"Contents/Resources/{ICON}"] = (icon, 0o644)
    return files


def read_info(bundle: Path) -> dict[str, Any] | None:
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_ours(bundle: Path) -> bool:
    info = read_info(bundle)
    return bool(info) and info.get("CFBundleIdentifier") == LAUNCHER_ID and MARK in info


def find(folder: Path) -> list[Path]:
    """Our launchers in ``folder`` (one, unless the language changed since it was written)."""
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    return [p for p in entries if p.suffix == ".app" and p.is_dir() and not p.is_symlink() and is_ours(p)]


def _same(bundle: Path, files: Mapping[str, tuple[bytes, int]]) -> bool:
    """``bundle`` is exactly these files already (nothing to write: Spotlight need not index it again)."""
    try:
        present = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
        if present != set(files):
            return False
        for rel, (data, mode) in files.items():
            path = bundle / rel
            if path.read_bytes() != data or (os.name == "posix" and path.stat().st_mode & 0o777 != mode):
                return False
    except OSError:
        return False
    return True


def _write(folder: Path, name: str, files: Mapping[str, tuple[bytes, int]]) -> None:
    """Build the bundle next to where it goes, then swap it in: Spotlight never sees half a launcher."""
    target = folder / f"{name}.app"
    staging = Path(tempfile.mkdtemp(prefix=".voicestream-launcher-", dir=folder))
    try:
        new = staging / f"{name}.app"
        for rel, (data, mode) in files.items():
            path = new / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            with contextlib.suppress(OSError):  # a volume without Unix permissions
                os.chmod(path, mode)
        old = staging / "previous.app"
        if target.exists():
            os.replace(target, old)
        os.replace(new, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def sync(folder: Path, launcher: Launcher | None) -> str:
    """Make ``folder`` hold the launcher ``launcher`` describes, or none (``None``: the setting is off).

    Returns ``"written"``, ``"unchanged"``, ``"removed"`` (there was one, and the setting is off),
    ``"absent"`` (none, and none wanted) or ``"blocked"`` (something that isn't ours has its name: left
    alone). Raises ``OSError`` when the folder can't be written."""
    ours = find(folder)
    if launcher is None:
        for bundle in ours:
            shutil.rmtree(bundle)
        return "removed" if ours else "absent"
    target = folder / f"{launcher.name}.app"
    if (target.exists() or target.is_symlink()) and target not in ours:
        log.warning("not writing the Spotlight launcher: %s is not Voicestream's", target)
        return "blocked"
    files = _files(launcher)
    for bundle in ours:
        if bundle != target:  # the other language's name
            shutil.rmtree(bundle)
    if target in ours and _same(target, files):
        return "unchanged"
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder, launcher.name, files)
    log.info("wrote the Spotlight launcher %s", target.name)
    return "written"


def check(bundle: Path, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """For the self-test: is ``bundle`` a launcher macOS can open? Its Info.plist reads (with plutil where
    there is one, as macOS reads it), it is ours, its executable is a script that may run and opens a keyed
    link, readable by you only. Only booleans: the report is shared, the script holds the key."""
    info = read_info(bundle) or {}
    exe = bundle / "Contents" / "MacOS" / str(info.get("CFBundleExecutable") or EXECUTABLE)
    try:
        text = exe.read_text(encoding="utf-8")
    except OSError:
        text = ""
    out: dict[str, Any] = {
        "plist": bool(info) and info.get("CFBundlePackageType") == "APPL",
        "ours": is_ours(bundle),
        "background_only": info.get("LSUIElement") is True,
        "executable": exe.is_file() and os.access(exe, os.X_OK),
        "script": text.startswith("#!/bin/sh\n") and "/usr/bin/open -g" in text and LINK_PREFIX in text,
        "private": os.name != "posix" or (exe.is_file() and exe.stat().st_mode & 0o077 == 0),
    }
    if Path(PLUTIL).is_file():
        try:
            lint = run([PLUTIL, "-lint", str(bundle / "Contents" / "Info.plist")], capture_output=True, text=True,
                       timeout=20, check=False)
            out["plutil"] = lint.returncode == 0
        except (OSError, subprocess.SubprocessError):
            out["plutil"] = False
    out["ok"] = all(out.values())
    return out
