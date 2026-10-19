"""Small macOS services the app backend uses when it can: microphone permission and launch at login.

Every function is safe to call anywhere. Off macOS, outside the packaged app, or without the
PyObjC frameworks (a terminal install without the ``mac`` extra) they answer "I can't tell"
(``None``) or ``"unsupported"`` instead of raising, so callers need no platform checks.

* Microphone: AVFoundation's authorization status, and the request that shows macOS's prompt.
  There is no such API for system audio (Core Audio taps): the backend detects a missing
  permission from a short capture that comes back as pure digital silence.
* Launch at login: ``SMAppService.mainAppService`` (macOS 13+), which needs the real app bundle.
  An ad-hoc signed build gets a new code identity with every update and the registration goes
  stale, so it is redone when the build changes (:func:`refresh_login_item`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import plistlib
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# AVAuthorizationStatus -> our words (granted/denied/undetermined/restricted)
_MIC_STATUS = {0: "undetermined", 1: "restricted", 2: "denied", 3: "granted"}
# SMAppServiceStatus
_LOGIN_STATUS = {0: "disabled", 1: "enabled", 2: "requires_approval", 3: "not_found"}
LOGIN_ITEMS_URL = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


def app_support_dir() -> Path:
    """``~/Library/Application Support/Voicestream``: the shell's own small files (not settings)."""
    return Path("~/Library/Application Support/Voicestream").expanduser()


# ----------------------------------------------------------------- microphone


def _avfoundation() -> Any | None:
    if sys.platform != "darwin":
        return None
    try:
        import AVFoundation
    except ImportError:
        return None
    return AVFoundation


def microphone_authorization() -> str | None:
    """``"granted"``, ``"denied"``, ``"undetermined"`` (never asked) or ``"restricted"`` (blocked by a
    profile); ``None`` when this can't be known (not macOS, or AVFoundation isn't installed).

    Outside the packaged app macOS answers for the terminal the app was started from."""
    av = _avfoundation()
    if av is None:
        return None
    try:
        status = av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeAudio)
    except Exception as exc:  # noqa: BLE001 - a broken framework must not break the settings page
        log.warning("could not read the microphone permission: %s", exc)
        return None
    return _MIC_STATUS.get(int(status))


def request_microphone(timeout: float = 60.0) -> str | None:
    """Ask for the microphone: macOS shows its prompt the first time only, later calls just
    return the current answer. Blocks until the person answers or ``timeout`` (then
    ``"undetermined"``), so call it from a worker thread, never from the main thread.

    Returns what :func:`microphone_authorization` returns."""
    av = _avfoundation()
    if av is None:
        return None
    status = microphone_authorization()
    if status != "undetermined":
        return status
    answered = threading.Event()
    result: dict[str, bool] = {}

    def handler(granted: bool) -> None:  # called on one of AVFoundation's queues
        result["granted"] = bool(granted)
        answered.set()

    try:
        av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(av.AVMediaTypeAudio, handler)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not ask for the microphone: %s", exc)
        return microphone_authorization()
    if not answered.wait(timeout):
        return microphone_authorization()
    return "granted" if result.get("granted") else "denied"


def microphone_status() -> str:
    """:func:`microphone_authorization` for :mod:`voicestream.app.host`'s optional hook, which
    treats an exception as "can't tell"."""
    status = microphone_authorization()
    if status is None:
        raise RuntimeError("the microphone permission can only be read on a Mac with PyObjC")
    return status


# ------------------------------------------------------------- launch at login


class LoginItemError(RuntimeError):
    """macOS refused to (un)register the app as a login item; the message is for people."""


def bundle_path() -> Path | None:
    """The running ``Voicestream.app``; ``None`` outside the packaged app (a terminal install),
    where registering "the main app" would register Python itself."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    app = Path(sys.executable).resolve().parents[2]  # <app>/Contents/MacOS/Voicestream
    return app if app.suffix == ".app" else None


def app_location_problem() -> str | None:
    """Why launch at login may not work from where the app is, or ``None``.

    macOS runs a downloaded app that wasn't moved from a random read-only copy (App
    Translocation), and a login item registered from there points at nothing after a restart."""
    app = bundle_path()
    if app is None:
        return None
    where = str(app)
    if "/AppTranslocation/" in where:
        return (
            "Voicestream is running from a temporary copy macOS made of the download: "
            "drag it to the Applications folder and open it from there."
        )
    applications = (Path("/Applications"), Path.home() / "Applications")
    if not any(app.parent == d or d in app.parents for d in applications):
        return "Move Voicestream to the Applications folder so it can open at login."
    return None


def _service() -> Any | None:
    if bundle_path() is None:
        return None
    try:
        from ServiceManagement import SMAppService
    except ImportError:
        return None
    return SMAppService.mainAppService()


def login_item_status() -> str:
    """``"enabled"``, ``"disabled"``, ``"requires_approval"`` (the person must allow it in System
    Settings → General → Login Items), ``"not_found"`` or ``"unsupported"``."""
    svc = _service()
    if svc is None:
        return "unsupported"
    try:
        return _LOGIN_STATUS.get(int(svc.status()), "not_found")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the login item status: %s", exc)
        return "not_found"


def open_login_items_settings() -> None:
    """System Settings → General → Login Items."""
    with contextlib.suppress(Exception):
        from ServiceManagement import SMAppService

        SMAppService.openSystemSettingsLoginItems()
        return
    if sys.platform == "darwin":
        with contextlib.suppress(OSError):
            subprocess.Popen(["/usr/bin/open", LOGIN_ITEMS_URL])  # noqa: S603


def set_login_item(enabled: bool) -> str:
    """Open (or stop opening) Voicestream when you log in; returns :func:`login_item_status`.

    Raises :class:`LoginItemError` when macOS refuses. When macOS wants your approval first, the
    Login Items settings are opened for you and ``"requires_approval"`` is returned."""
    svc = _service()
    if svc is None:
        return "unsupported"
    if enabled:
        problem = app_location_problem()
        if problem:
            log.warning("launch at login: %s", problem)
        if login_item_status() == "enabled" and _stored_build() == build_identity():
            return "enabled"
        _register(svc)
    else:
        if login_item_status() in ("disabled", "not_found"):
            _forget_build()
            return login_item_status()
        ok, err = svc.unregisterAndReturnError_(None)
        if not ok:
            raise LoginItemError(f"macOS refused to remove Voicestream from the login items: {_describe(err)}")
        _forget_build()
    status = login_item_status()
    if enabled and status == "requires_approval":
        open_login_items_settings()
    return status


def set_start_at_login(enabled: bool) -> None:
    """:func:`set_login_item` for :meth:`voicestream.app.host.AppHost.update_prefs`'s hook, which
    expects an exception whenever the choice can't take effect."""
    if set_login_item(enabled) == "unsupported":
        raise LoginItemError("opening at login works only in the Voicestream app, installed in Applications")


def refresh_login_item() -> str:
    """Re-register after an update: an ad-hoc signed build's registration is tied to the old build.

    Call once at launch; returns :func:`login_item_status`."""
    status = login_item_status()
    if status not in ("enabled", "requires_approval") or _stored_build() == build_identity():
        return status
    svc = _service()
    if svc is None:  # pragma: no cover - status() said it exists
        return status
    log.info("the app changed since launch at login was set up: registering it again")
    with contextlib.suppress(Exception):
        svc.unregisterAndReturnError_(None)
    try:
        _register(svc)
    except LoginItemError as exc:
        log.warning("%s", exc)
    return login_item_status()


def _register(svc: Any) -> None:
    ok, err = svc.registerAndReturnError_(None)
    if not ok:
        raise LoginItemError(f"macOS refused to open Voicestream at login: {_describe(err)}")
    _remember_build()


def _describe(err: Any) -> str:
    if err is None:
        return "unknown error"
    try:
        return str(err.localizedDescription())
    except Exception:  # noqa: BLE001
        return str(err)


def build_identity() -> str | None:
    """This build of the app: its version plus the executable's timestamp (every build differs)."""
    app = bundle_path()
    if app is None:
        return None
    try:
        with (app / "Contents" / "Info.plist").open("rb") as fh:
            version = plistlib.load(fh).get("CFBundleVersion", "?")
        mtime = Path(sys.executable).resolve().stat().st_mtime_ns
    except (OSError, plistlib.InvalidFileException):
        return None
    return f"{version}:{mtime}"


def _state_file() -> Path:
    return app_support_dir() / "login-item.json"


def _stored_build() -> str | None:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8")).get("build")
    except (OSError, ValueError, AttributeError):
        return None


def _remember_build() -> None:
    from .files import write_private

    try:
        write_private(_state_file(), json.dumps({"build": build_identity()}) + "\n")
    except OSError as exc:
        log.warning("could not remember the login item build: %s", exc)


def _forget_build() -> None:
    with contextlib.suppress(OSError):
        _state_file().unlink()
