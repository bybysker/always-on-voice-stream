"""The Spotlight launcher (voicestream.app.spotlight), written into temporary folders: never ~/Applications.

The packaged app's ``--selftest`` writes one into a temporary folder on the macOS CI runner too, and has
``plutil`` read its Info.plist.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import types
from pathlib import Path

import pytest

from voicestream.app import spotlight

URL = "voicestream://meeting/start?key=" + "k" * 43


def _launcher(tmp_path: Path, **kw: object) -> spotlight.Launcher:
    return spotlight.Launcher(url=URL, **{"version": "0.3.0", **kw})  # type: ignore[arg-type]


def _bundle(folder: Path, lang: str = "en") -> Path:
    return folder / f"{spotlight.NAMES[lang]}.app"


def test_write_then_keep_then_remove(tmp_path: Path) -> None:
    folder = tmp_path / "Applications"
    assert spotlight.sync(folder, None) == "absent"
    assert spotlight.sync(folder, _launcher(tmp_path)) == "written"
    bundle = _bundle(folder)
    assert bundle.name == "Voicestream – New Meeting.app"
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "io.github.bybysker.voicestream.new-meeting"  # its own, not the app's
    assert info["CFBundleIdentifier"] != spotlight.APP_ID
    assert info["LSUIElement"] is True and info["CFBundlePackageType"] == "APPL"
    assert info["CFBundleExecutable"] == "new-meeting" and info["CFBundleVersion"] == "0.3.0"
    assert "CFBundleIconFile" not in info  # no icon given
    assert (bundle / "Contents" / "PkgInfo").read_bytes() == b"APPL????"
    exe = bundle / "Contents" / "MacOS" / "new-meeting"
    if os.name == "posix":
        assert exe.stat().st_mode & 0o777 == 0o700  # it holds the key
    assert spotlight.check(bundle, run=lambda *a, **k: types.SimpleNamespace(returncode=0))["ok"] is True
    mtime = exe.stat().st_mtime_ns
    assert spotlight.sync(folder, _launcher(tmp_path)) == "unchanged"  # nothing for Spotlight to index again
    assert exe.stat().st_mtime_ns == mtime
    assert [p.name for p in folder.iterdir()] == [bundle.name]  # no staging folder left behind
    assert spotlight.sync(folder, None) == "removed"
    assert not bundle.exists() and spotlight.sync(folder, None) == "absent"


def test_the_script_opens_the_keyed_link_in_the_background(tmp_path: Path) -> None:
    app = Path("/Users/me/Applications/Voice stream's.app")
    text = spotlight.script(URL, app)
    assert text.startswith("#!/bin/sh\n")
    run = text.strip().splitlines()[-1]
    # the copy that wrote it, then any Voicestream by its bundle id (moved, or reinstalled elsewhere)
    assert run == (f"/usr/bin/open -g -a '/Users/me/Applications/Voice stream'\"'\"'s.app' '{URL}' 2>/dev/null || "
                   f"exec /usr/bin/open -g -b io.github.bybysker.voicestream '{URL}'")
    assert spotlight.script(URL).strip().splitlines()[-1] == f"exec /usr/bin/open -g -b io.github.bybysker.voicestream '{URL}'"
    assert "k" * 43 not in repr(spotlight.Launcher(url=URL))  # never in a log line


@pytest.mark.skipif(os.name != "posix", reason="runs the launcher's shell script")
def test_the_script_runs(tmp_path: Path) -> None:
    """With a stand-in for /usr/bin/open: the first open fails (the app moved), the fallback by id runs."""
    folder = tmp_path / "Applications"
    spotlight.sync(folder, _launcher(tmp_path, app=tmp_path / "Voicestream.app"))
    exe = _bundle(folder) / "Contents" / "MacOS" / "new-meeting"
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "opened.txt"
    text = exe.read_text().replace("/usr/bin/open", str(fake / "open"))
    (fake / "open").write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$3" != "{tmp_path / "Voicestream.app"}" ]\n')
    (fake / "open").chmod(0o755)
    script = tmp_path / "run.sh"
    script.write_text(text)
    out = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, timeout=20, check=False)
    assert out.returncode == 0, out.stderr
    lines = log.read_text().splitlines()
    assert lines == [f"-g -a {tmp_path / 'Voicestream.app'} {URL}", f"-g -b io.github.bybysker.voicestream {URL}"]


def test_the_app_icon_is_carried_over(tmp_path: Path) -> None:
    app = tmp_path / "Voicestream.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": "Voicestream.icns"}))
    (app / "Contents" / "Resources" / "Voicestream.icns").write_bytes(b"icns-data")
    icon = spotlight.app_icon(app)
    assert icon == app / "Contents" / "Resources" / "Voicestream.icns"
    folder = tmp_path / "Applications"
    spotlight.sync(folder, _launcher(tmp_path, app=app, icon=icon))
    bundle = _bundle(folder)
    assert (bundle / "Contents" / "Resources" / "AppIcon.icns").read_bytes() == b"icns-data"
    assert plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())["CFBundleIconFile"] == "AppIcon.icns"
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": "Missing"}))
    assert spotlight.app_icon(app) is None and spotlight.app_icon(None) is None
    (app / "Contents" / "Info.plist").write_bytes(b"not a plist")
    assert spotlight.app_icon(app) is None


def test_a_new_key_language_or_place_rewrites_it(tmp_path: Path) -> None:
    folder = tmp_path / "Applications"
    spotlight.sync(folder, _launcher(tmp_path))
    other = spotlight.Launcher(url=URL.replace("k" * 43, "n" * 43), version="0.3.0")
    assert spotlight.sync(folder, other) == "written"
    assert "n" * 43 in (_bundle(folder) / "Contents" / "MacOS" / "new-meeting").read_text()
    assert spotlight.sync(folder, _launcher(tmp_path, app=tmp_path / "Elsewhere.app")) == "written"
    assert spotlight.sync(folder, _launcher(tmp_path, lang="fr")) == "written"  # renamed: one launcher, not two
    assert [p.name for p in folder.iterdir()] == ["Voicestream – Nouvelle réunion.app"]
    assert spotlight.sync(folder, None) == "removed" and list(folder.iterdir()) == []


def test_a_folder_of_the_same_name_that_is_not_ours_is_left_alone(tmp_path: Path) -> None:
    folder = tmp_path / "Applications"
    theirs = _bundle(folder)
    (theirs / "Contents").mkdir(parents=True)
    (theirs / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
    assert spotlight.sync(folder, _launcher(tmp_path)) == "blocked"
    assert spotlight.sync(folder, None) == "absent"  # never removed either
    assert plistlib.loads((theirs / "Contents" / "Info.plist").read_bytes())["CFBundleIdentifier"] == "com.example.other"
    assert not spotlight.is_ours(theirs) and spotlight.find(tmp_path / "nowhere") == []


def test_check_says_what_is_wrong(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "Applications"
    spotlight.sync(folder, _launcher(tmp_path))
    bundle = _bundle(folder)
    linted: list[list[str]] = []
    monkeypatch.setattr(spotlight, "PLUTIL", str(Path(__file__)))  # "there is a plutil"
    ok = spotlight.check(bundle, run=lambda argv, **k: linted.append(argv) or types.SimpleNamespace(returncode=0))
    assert ok["ok"] and ok["plutil"] and linted[0][1:] == ["-lint", str(bundle / "Contents" / "Info.plist")]
    bad = spotlight.check(bundle, run=lambda argv, **k: types.SimpleNamespace(returncode=1))
    assert bad["plutil"] is False and bad["ok"] is False
    assert set(bad) == {"plist", "ours", "background_only", "executable", "script", "private", "plutil", "ok"}
    assert all(isinstance(v, bool) for v in bad.values())  # booleans only: the report is shared, the script holds the key
    if os.name == "posix":
        (bundle / "Contents" / "MacOS" / "new-meeting").chmod(0o644)
        assert spotlight.check(bundle, run=lambda *a, **k: types.SimpleNamespace(returncode=0))["executable"] is False


def test_the_real_folder_is_only_named() -> None:
    assert spotlight.applications_dir() == Path.home() / "Applications"
