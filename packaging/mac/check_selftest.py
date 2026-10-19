"""Did the packaged app's self-test pass? (``packaging/mac/build.sh selftest`` runs this.)

    python packaging/mac/check_selftest.py <selftest.json> <Voicestream.app>

The app's own report says ``ok`` when every check it ran passed, which is not enough here:

* an app that fell back to the browser (pywebview, a PyObjC framework or PyObjCTools missing from
  the bundle) runs only the two HTTP checks, passes them, and would ship with no window and no
  menu-bar icon;
* the whisper-server it finds must be the copy inside the app: CI exports the build tree's copy as
  ``$VOICESTREAM_WHISPER_SERVER``, which ``find_binary`` reads first, and that says nothing about
  what users get;
* the keyboard shortcut checks only run on a Mac: macOS must have taken it (RegisterEventHotKey
  answered noErr) and the app's state must say so, a press delivered to the app must reach the host
  and change nothing, and the menu must show it;
* the keyboard layout must be read from macOS (what each key types, by which Settings shows a
  shortcut recorded with ⌥ held): a character of its own for each letter key — on the CI runners'
  US layout the US one, KeyQ typing q — and told to the host.

Prints the report and every problem; exits 1 when there is one. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

NATIVE_CHECKS = (
    "window_loaded", "page_rendered", "page_has_content", "app_page", "api_app", "status_item",
    "app_delegate_patched", "hide_on_close_and_reopen", "whisper_server_bundled",
    "hotkey_registered", "hotkey_reaches_host", "hotkey_in_menu", "keyboard_layout",
)  # fmt: skip
"""What native mode checks in the packaged app (voicestream/app/native.py MacShell._selftest)."""


def _inside(path: str, app: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(app.resolve())
    except (OSError, ValueError):
        return False


def problems(report: dict[str, Any], app: Path) -> list[str]:
    """Why this report is not a pass, in words (empty: it passed)."""
    out: list[str] = []
    mode = report.get("mode")
    if mode != "native":
        out.append(f"the app did not open its own window (mode: {mode}): it fell back to the browser")
    if not report.get("frozen"):
        out.append("the report is not from the packaged app (frozen: false)")
    checks = report.get("checks") or {}
    missing = [name for name in NATIVE_CHECKS if name not in checks]
    if missing:
        out.append("checks that did not run: " + ", ".join(missing))
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        out.append("failed checks: " + ", ".join(failed))
    ws = report.get("whisper_server")
    if not ws or not _inside(str(ws), app):
        out.append(f"the app did not find its own whisper-server inside {app.name} (found: {ws})")
    hk = report.get("hotkey") or {}
    before, after = hk.get("state_before"), hk.get("state_after")
    if before != after:  # the test only asks the host what a press would do (set up, your Mac may be listening)
        out.append("the keyboard shortcut's test left the app listening" if after in ("starting", "listening", "paused")
                   else f"the keyboard shortcut's test changed the app's state ({before} → {after})")
    if hk.get("wanted") and hk.get("register_result") == 0 and hk.get("host_status") != "ok":
        out.append(f"macOS took the keyboard shortcut, but the app's state says {hk.get('host_status')!r}: "
                   "the menu and Settings don't show it")
    for error in report.get("errors") or []:
        out.append(f"error: {error}")
    if "quit" not in report:
        out.append("the app did not finish quitting (its last report, written as it quits, is missing)")
    if not report.get("ok") and not out:
        out.append("the app's own report says it failed")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_selftest.py <selftest.json> <Voicestream.app>", file=sys.stderr)
        return 2
    report_path, app = Path(argv[0]), Path(argv[1])
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: no self-test report at {report_path}: {exc}")
        return 1
    print(json.dumps(report, indent=2))
    found = problems(report, app)
    for line in found:
        print("self-test:", line)
    print("self-test:", "FAILED" if found else "passed")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
