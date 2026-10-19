"""What goes into Voicestream.app's Info.plist, and the build inputs the PyInstaller spec needs.

Kept out of ``voicestream.spec`` so it can be checked without PyInstaller or a Mac
(tests/test_mac_packaging.py). Also a tiny command line for build.sh::

    python packaging/mac/bundle_info.py version   # the package version (DMG name)
    python packaging/mac/bundle_info.py plist     # the Info.plist keys, as XML
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
from collections.abc import Mapping
from importlib.util import find_spec
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APP_NAME = "Voicestream"
BUNDLE_ID = "io.github.bybysker.voicestream"
"""Never change it: macOS keys the microphone and system-audio permissions, the Keychain items
and launch at login on it, so a new id is a new app that has to be allowed again."""
MIN_MACOS = "14.2"
"""Core Audio process taps (the other participants' audio) need macOS 14.2."""

WHISPER_ENV = "VOICESTREAM_WHISPER_SERVER"
"""The whisper-server to bundle; the same variable points voicestream and its tests at a binary."""
ALLOW_NO_WHISPER_ENV = "VOICESTREAM_ALLOW_NO_WHISPER"
AGENT_CLI_ENV = "VOICESTREAM_BUNDLE_AGENT_CLI"
ICON_ENV = "VOICESTREAM_ICON"

# One string per key, French then English: macOS shows it as is in the permission prompt.
MICROPHONE = (
    "Voicestream écoute votre micro pendant vos réunions pour transcrire ce que vous dites. — "
    "Voicestream listens to your microphone during meetings to transcribe what you say."
)
SYSTEM_AUDIO = (
    "Voicestream capte le son de votre Mac pendant les réunions pour transcrire les autres participants. — "
    "Voicestream captures your Mac's audio during meetings to transcribe the other participants."
)
APPLE_EVENTS = (
    "Voicestream ajoute vos tâches dans Rappels et lance les raccourcis que vous avez choisis. — "
    "Voicestream adds your tasks to Reminders and runs the shortcuts you chose."
)


def package_version(root: Path = ROOT) -> str:
    """``__version__`` from the source, without importing the package."""
    text = (root / "src" / "voicestream" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit("no __version__ in src/voicestream/__init__.py")
    return match.group(1)


def info_plist(version: str) -> dict[str, Any]:
    """Merged by PyInstaller's BUNDLE into the Info.plist it writes (which already has the
    executable, the icon file and the package type)."""
    numeric = re.match(r"\d+(\.\d+){0,2}", version)
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": numeric.group(0) if numeric else "0",  # macOS wants 1-3 integers here
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": ["en", "fr"],  # system dialogs (Open, Save) follow the Mac's language
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSUIElement": False,  # a normal app with a Dock icon; the shell hides it when the window closes
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # dark mode
        "NSSupportsAutomaticTermination": False,  # it listens all day with no window: never quit it behind our back
        "NSSupportsSuddenTermination": False,
        "NSHumanReadableCopyright": "Voicestream — MIT licence",
        "NSMicrophoneUsageDescription": MICROPHONE,
        "NSAudioCaptureUsageDescription": SYSTEM_AUDIO,
        "NSAppleEventsUsageDescription": APPLE_EVENTS,
        # The window shows http://127.0.0.1:<port>: plain HTTP to an IP address needs these since macOS 14
        "NSAppTransportSecurity": {
            "NSAllowsLocalNetworking": True,
            "NSExceptionDomains": {
                host: {"NSExceptionAllowsInsecureHTTPLoads": True}
                for host in ("127.0.0.1", "localhost")
            },
        },
    }


def whisper_server(env: Mapping[str, str] = os.environ) -> Path | None:
    """The whisper-server binary to put in ``Contents/Frameworks/bin`` (through the spec: a
    binary added after the build would break the bundle's signature)."""
    value = env.get(WHISPER_ENV)
    if not value:
        if env.get(ALLOW_NO_WHISPER_ENV) == "1":
            return None
        raise SystemExit(
            f"set {WHISPER_ENV} to the whisper-server to bundle (packaging/mac/build.sh whisper builds it), "
            f"or {ALLOW_NO_WHISPER_ENV}=1 for an app without on-device speech-to-text"
        )
    path = Path(value).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SystemExit(f"{WHISPER_ENV}={value} is not an executable file")
    return path


def agent_cli(env: Mapping[str, str] = os.environ) -> Path | None:
    """The Claude Code CLI that claude-agent-sdk ships (about 200 MB), only when asked for with
    ``VOICESTREAM_BUNDLE_AGENT_CLI=1``. Without it the full agent engine uses an installed
    ``claude``, and ``agents.engine = "api"`` needs nothing."""
    if env.get(AGENT_CLI_ENV) != "1":
        return None
    spec = find_spec("claude_agent_sdk")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(f"{AGENT_CLI_ENV}=1 but claude-agent-sdk is not installed")
    cli = Path(next(iter(spec.submodule_search_locations))) / "_bundled" / "claude"
    if not cli.is_file():
        raise SystemExit(f"{AGENT_CLI_ENV}=1 but this claude-agent-sdk has no bundled CLI ({cli})")
    return cli


def icon(env: Mapping[str, str] = os.environ, here: Path = HERE) -> Path | None:
    """The .icns made by make_icon.py (PyInstaller's default icon when there is none)."""
    candidates = [env.get(ICON_ENV), str(here / "build" / f"{APP_NAME}.icns")]
    for c in candidates:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser().resolve()
    return None


def main(argv: list[str]) -> int:
    what = argv[0] if argv else "version"
    version = package_version()
    if what == "version":
        print(version)
    elif what == "plist":
        sys.stdout.buffer.write(plistlib.dumps(info_plist(version)))  # UTF-8 bytes, whatever the console
    else:
        print("usage: bundle_info.py [version|plist]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
