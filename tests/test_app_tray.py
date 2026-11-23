"""The Windows app's icon next to the clock (voicestream/app/tray.py), with a fake pystray.

The fake keeps what pystray's Windows backend does that matters here: ``run(setup)`` calls ``setup``
in a thread and returns once ``stop()`` is called; the menu is a callable asked again on
``update_menu()``; ``title`` and ``icon`` are properties the app sets.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from voicestream.app import launcher, single, tray

from test_mac_app import FakeHost

pytest.importorskip("PIL")


class FakeMenuItem:
    def __init__(self, text: str, action: Any, *, enabled: bool = True, default: bool = False) -> None:
        self.text, self.action, self.enabled, self.default = text, action, enabled, default


class FakeMenu:
    SEPARATOR = "---"

    def __init__(self, items: Any) -> None:
        self.items = items

    def build(self) -> list[Any]:
        return list(self.items())


class FakeIcon:
    instances: list[FakeIcon] = []

    def __init__(self, name: str, *, icon: Any, title: str, menu: FakeMenu) -> None:
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.visible = False
        self.menus: list[list[Any]] = [menu.build()]
        self._stopped = threading.Event()
        FakeIcon.instances.append(self)

    def update_menu(self) -> None:
        self.menus.append(self.menu.build())

    def run(self, setup: Any) -> None:
        threading.Thread(target=setup, args=(self,), daemon=True).start()
        assert self._stopped.wait(30), "the icon was never stopped"
        self.visible = False

    def stop(self) -> None:
        self._stopped.set()

    def click(self, title: str) -> None:
        item = next(i for i in self.menus[-1] if i != FakeMenu.SEPARATOR and i.text == title)
        assert item.enabled, f"{title} is greyed out"
        item.action(self, item)


class FakePystray:
    Icon, Menu, MenuItem = FakeIcon, FakeMenu, FakeMenuItem


NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def titles(entries: list[tray.Entry]) -> list[str]:
    return [e.title for e in entries if e.visible and not e.separator]


def meeting_state(minutes: int = 12) -> dict[str, Any]:
    started = (NOW - timedelta(minutes=minutes)).isoformat()
    return {"state": "listening", "in_meeting": True, "meeting": {"started_at": started},
            "mode": "manual", "prefs": {"meeting_start": "manual"}}


def test_menu_follows_the_meeting() -> None:
    idle = {"state": "stopped", "in_meeting": False, "mode": "manual", "prefs": {"meeting_start": "manual"}}
    assert titles(tray.menu_entries(idle, "fr", NOW)) == [
        "Aucune réunion en cours", "Ouvrir Voicestream", "Démarrer une réunion", "Quitter Voicestream"]
    assert titles(tray.menu_entries(meeting_state(), "en", NOW)) == [
        "Meeting in progress · 12 min", "Open Voicestream", "Stop Meeting", "Quit Voicestream"]
    opened = next(e for e in tray.menu_entries(idle, "en", NOW) if e.key == "open")
    assert opened.default  # a left click on the icon opens the app
    quitting = tray.menu_entries(meeting_state(), "en", NOW, quitting=True)
    assert titles(quitting)[0].startswith("Quitting") and not any(e.enabled for e in quitting if not e.separator)


def test_tooltip_and_red_dot() -> None:
    assert tray.tooltip(meeting_state(5), "fr", NOW) == "Voicestream · Réunion en cours · 5 min"
    assert tray.recording(meeting_state()) and not tray.recording({"state": "stopped"})
    assert len(tray.tooltip({"state": "error", "error": "x" * 400}, "en", NOW)) <= tray.TOOLTIP_MAX
    plain, dot = tray.draw_icon(), tray.draw_icon(dot=True)
    assert plain.size == dot.size == (tray.ICON_PX, tray.ICON_PX) and plain.mode == "RGBA"
    r, g, b, _ = dot.getpixel((tray.ICON_PX - 10, tray.ICON_PX - 10))
    assert r > 200 and g < 120  # the dot is red, bottom right
    assert plain.getpixel((0, 0))[3] == 0  # rounded corners: transparent


def test_tray_actions_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    host = FakeHost()
    host._state.update({"mode": "manual", "prefs": {"ui_language": "en", "meeting_start": "manual"}})
    url = host.run_in_thread()
    opened: list[str] = []
    t = tray.Tray(host, url, FakePystray, open_url=opened.append, languages=lambda: [])
    icon = t.icon
    menu = [i.text for i in icon.menus[-1] if i != FakeMenu.SEPARATOR]
    assert menu == ["No meeting in progress", "Open Voicestream", "Start Meeting", "Quit Voicestream"]
    icon.click("Open Voicestream")
    assert opened == [url]
    icon.click("Start Meeting")
    t.refresh(meeting_state())  # the host says a meeting started: the dot, the tooltip, the menu follow
    assert "Meeting in progress" in icon.title and "Stop Meeting" in [getattr(i, "text", "") for i in icon.menus[-1]]
    assert icon.icon.getpixel((tray.ICON_PX - 10, tray.ICON_PX - 10))[0] > 200
    icon.click("Stop Meeting")
    t.quit()
    assert t.wait_stopped(10) and t.graceful is True
    assert icon._stopped.is_set()
    for _ in range(50):
        if ("meeting_stop", None) in host.calls or any(c[0] == "meeting_stop" for c in host.calls):
            break
        threading.Event().wait(0.05)
    names = [c[0] for c in host.calls]
    assert "meeting_start" in names and "meeting_stop" in names and "shutdown" in names


def test_language_from_windows_when_the_app_has_none() -> None:
    host = FakeHost()
    host.run_in_thread()
    t = tray.Tray(host, "http://x", FakePystray, open_url=lambda u: None, languages=lambda: ["fr-FR"])
    assert t.lang == "fr" and t.icon.title.startswith("Voicestream · ")
    t.quit()
    assert t.wait_stopped(10)


def test_selftest_quits_through_the_menu(tmp_path: Path) -> None:
    host = FakeHost()
    report = tmp_path / "selftest.json"
    opened: list[str] = []
    inst = single.Instance(tmp_path / "home")
    assert tray.run(host, selftest=0.5, report_path=report, pystray=FakePystray, open_url=opened.append,
                    instance=inst) == 0
    assert not inst.held and not inst.info_path.exists()  # the lock and the address go with the app
    data = json.loads(report.read_text())
    assert data["ok"] and data["mode"] == "tray"
    assert data["checks"]["tray_icon"] and data["checks"]["tray_menu"] and data["checks"]["app_page"]
    assert data["quit"]["graceful"] is True
    assert opened == []  # a self-test opens no browser
    assert (tmp_path / "selftest.json.phase").read_text().strip() == "done"
    assert ("shutdown", False) in host.calls


def test_start_failure_is_reported(tmp_path: Path) -> None:
    host = FakeHost(fail=RuntimeError("port in use"))
    report = tmp_path / "r.json"
    inst = single.Instance(tmp_path / "home")
    assert tray.run(host, selftest=1, report_path=report, pystray=FakePystray, instance=inst) == 2
    assert not inst.held
    assert json.loads(report.read_text())["errors"] == ["start: port in use"]


@pytest.mark.parametrize(("platform", "available", "mode"), [
    ("win32", True, "tray"), ("win32", False, "browser"), ("linux", True, "browser"),
])
def test_windows_opens_with_the_icon(monkeypatch: pytest.MonkeyPatch, platform: str, available: bool, mode: str) -> None:
    monkeypatch.setattr(launcher, "tray_available", lambda: available)
    monkeypatch.setattr(launcher, "native_available", lambda: False)
    assert launcher.choose_mode(launcher.build_parser().parse_args([]), platform) == mode
    assert launcher.choose_mode(launcher.build_parser().parse_args(["--browser"]), platform) == "browser"


# ------------------------------------------------------------------ one copy at a time (single.py)


def test_one_copy_at_a_time(tmp_path: Path) -> None:
    first, second = single.Instance(tmp_path), single.Instance(tmp_path)
    assert first.acquire() and not second.acquire()
    assert second.running_url(wait=0) is None  # the first copy hasn't said where it is yet
    first.publish("http://127.0.0.1:5123/app?token=k")
    assert second.running_url(wait=0) == "http://127.0.0.1:5123/app?token=k"
    if os.name == "posix":
        assert first.info_path.stat().st_mode & 0o077 == 0  # it holds the app's key: yours only
    first.release()
    assert not first.info_path.exists() and second.acquire()  # once it quits, the next copy starts
    second.release()


def test_a_crashed_copys_address_is_never_followed(tmp_path: Path) -> None:
    (tmp_path / single.INFO_NAME).write_text(json.dumps({"url": "http://127.0.0.1:1/app"}))
    inst = single.Instance(tmp_path)
    assert inst.acquire() and not inst.info_path.exists()
    inst.release()


def test_only_a_local_address_is_opened(tmp_path: Path) -> None:
    first, second = single.Instance(tmp_path), single.Instance(tmp_path)
    assert first.acquire()
    first.info_path.write_text(json.dumps({"url": "https://example.com/phish"}))
    assert second.running_url(wait=0) is None
    first.release()


def test_a_second_copy_shows_the_first_and_ends(tmp_path: Path) -> None:
    first = single.Instance(tmp_path)
    assert first.acquire()
    first.publish("http://127.0.0.1:5123/app?token=k")
    host = FakeHost()
    opened: list[str] = []
    assert tray.run(host, pystray=FakePystray, open_url=opened.append, instance=single.Instance(tmp_path)) == 0
    assert opened == ["http://127.0.0.1:5123/app?token=k"]
    assert host.auto_listen is None  # it never started its own host: no second microphone, no second transcript
    report = tmp_path / "second.json"
    assert tray.run(FakeHost(), selftest=1, report_path=report, pystray=FakePystray, open_url=opened.append,
                    instance=single.Instance(tmp_path)) == 0
    assert json.loads(report.read_text())["mode"] == "already-running" and len(opened) == 1  # a self-test opens nothing
    first.release()
