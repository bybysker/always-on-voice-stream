"""packaging/mac: the Info.plist, the PyInstaller spec, the icon, the CI speech and build scripts.

The spec is executed here with stand-ins for PyInstaller's build classes, so what it would hand
to PyInstaller (binaries, data, hidden imports, EXE/BUNDLE options) is checked without a Mac.
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import voicestream

ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / "packaging" / "mac"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, MAC / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


info = _load("bundle_info")
make_speech = _load("make_speech")


# ------------------------------------------------------------------ Info.plist


def test_info_plist() -> None:
    plist = info.info_plist("1.2.3")
    assert plist["CFBundleIdentifier"] == "io.github.bybysker.voicestream"
    assert plist["CFBundleName"] == plist["CFBundleDisplayName"] == "Voicestream"
    assert plist["CFBundleShortVersionString"] == plist["CFBundleVersion"] == "1.2.3"
    assert plist["LSMinimumSystemVersion"] == "14.2"
    assert plist["LSApplicationCategoryType"] == "public.app-category.productivity"
    assert plist["NSHighResolutionCapable"] is True and plist["LSUIElement"] is False
    for key in ("NSMicrophoneUsageDescription", "NSAudioCaptureUsageDescription", "NSAppleEventsUsageDescription"):
        french, english = plist[key].split(" — ")  # one string, French then English
        assert "Voicestream" in french and "Voicestream" in english
    assert "NSScreenCaptureUsageDescription" not in plist  # no such key (Apple's docs: 404)
    ats = plist["NSAppTransportSecurity"]
    assert ats["NSAllowsLocalNetworking"] is True
    assert set(ats["NSExceptionDomains"]) == {"127.0.0.1", "localhost"}
    assert all(d["NSExceptionAllowsInsecureHTTPLoads"] for d in ats["NSExceptionDomains"].values())
    # voicestream:// links (the Spotlight launcher's) open in this app; the scheme is the one the host follows
    (url_type,) = plist["CFBundleURLTypes"]
    assert url_type["CFBundleURLSchemes"] == ["voicestream"] and url_type["CFBundleTypeRole"] == "Viewer"
    assert url_type["CFBundleURLName"].startswith(plist["CFBundleIdentifier"])
    from voicestream.app import host, spotlight

    assert info.URL_SCHEME == host.URL_SCHEME and spotlight.APP_ID == info.BUNDLE_ID
    assert spotlight.MIN_MACOS == info.MIN_MACOS
    assert plistlib.loads(plistlib.dumps(plist)) == plist  # everything is plist-serializable


def test_bundle_version_is_numeric() -> None:
    assert info.info_plist("1.2.3rc1")["CFBundleVersion"] == "1.2.3"
    assert info.info_plist("1.2.3rc1")["CFBundleShortVersionString"] == "1.2.3rc1"


def test_version_comes_from_the_package() -> None:
    assert info.package_version() == voicestream.__version__
    out = subprocess.run([sys.executable, str(MAC / "bundle_info.py"), "version"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == voicestream.__version__
    xml = subprocess.run([sys.executable, str(MAC / "bundle_info.py"), "plist"], capture_output=True, check=True).stdout
    assert plistlib.loads(xml)["CFBundleIdentifier"] == info.BUNDLE_ID


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.skipif(os.name != "posix", reason="executable bits")
def test_whisper_server_input(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="VOICESTREAM_WHISPER_SERVER"):
        info.whisper_server({})
    assert info.whisper_server({"VOICESTREAM_ALLOW_NO_WHISPER": "1"}) is None
    (tmp_path / "plain").write_text("")
    with pytest.raises(SystemExit, match="not an executable"):
        info.whisper_server({"VOICESTREAM_WHISPER_SERVER": str(tmp_path / "plain")})
    ws = _executable(tmp_path / "whisper-server")
    assert info.whisper_server({"VOICESTREAM_WHISPER_SERVER": str(ws)}) == ws.resolve()


def test_agent_cli_only_when_asked(tmp_path: Path) -> None:
    assert info.agent_cli({}) is None


def test_icon_lookup(tmp_path: Path) -> None:
    assert info.icon({}, here=tmp_path) is None
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "Voicestream.icns").write_bytes(b"icns")
    assert info.icon({}, here=tmp_path) == (tmp_path / "build" / "Voicestream.icns").resolve()
    other = tmp_path / "other.icns"
    other.write_bytes(b"icns")
    assert info.icon({"VOICESTREAM_ICON": str(other)}, here=tmp_path) == other.resolve()


# ---------------------------------------------------------------- the spec file


class Target:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs
        self.pure, self.scripts, self.binaries, self.datas = ["pure"], ["scripts"], ["binaries"], ["datas"]


def _run_spec(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> dict[str, Any]:
    hooks = types.SimpleNamespace(
        collect_dynamic_libs=lambda pkg: [(f"/site/{pkg}/native/libcatap_coreaudio.dylib", f"{pkg}/native")],
        collect_data_files=lambda pkg: [(f"/site/{pkg}/dashboard/static/index.html", f"{pkg}/dashboard/static")],
        copy_metadata=lambda pkg: [(f"/site/{pkg}-0.dist-info", f"{pkg}-0.dist-info")],
        collect_submodules=lambda pkg, filter=lambda n: True: [  # noqa: A006
            n for n in (pkg, f"{pkg}.__main__", f"{pkg}.app.__main__", f"{pkg}.sinks.notion", f"{pkg}.app.native")
            if filter(n)
        ],
    )
    pyinstaller = types.ModuleType("PyInstaller")
    utils = types.ModuleType("PyInstaller.utils")
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", utils)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    monkeypatch.setitem(sys.modules, "bundle_info", info)  # the spec imports it from its own folder
    monkeypatch.setattr(sys, "path", list(sys.path))
    for key in ("VOICESTREAM_WHISPER_SERVER", "VOICESTREAM_ALLOW_NO_WHISPER", "VOICESTREAM_BUNDLE_AGENT_CLI", "VOICESTREAM_ICON"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    made: dict[str, Any] = {}

    def recorder(kind: str) -> Any:
        def make(*args: Any, **kwargs: Any) -> Target:
            made[kind] = t = Target(*args, **kwargs)
            return t

        return make

    namespace = {"SPECPATH": str(MAC), "__name__": "__main__"}
    namespace.update({k: recorder(k) for k in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")})
    exec(compile((MAC / "voicestream.spec").read_text(encoding="utf-8"), str(MAC / "voicestream.spec"), "exec"), namespace)  # noqa: S102
    return made


@pytest.mark.skipif(os.name != "posix", reason="executable bits")
def test_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = _executable(tmp_path / "whisper-server")
    made = _run_spec(monkeypatch, {"VOICESTREAM_WHISPER_SERVER": str(ws)})

    a = made["Analysis"]
    (script,) = a.args[0]
    assert Path(script) == ROOT / "src" / "voicestream" / "app" / "__main__.py" and Path(script).is_file()
    assert (str(ws.resolve()), "bin") in a.kwargs["binaries"]  # Contents/Frameworks/bin, where find_binary looks
    assert ("/site/catap/native/libcatap_coreaudio.dylib", "catap/native") in a.kwargs["binaries"]
    assert ("/site/voicestream/dashboard/static/index.html", "voicestream/dashboard/static") in a.kwargs["datas"]
    hidden = a.kwargs["hiddenimports"]
    for module in ("webview.platforms.cocoa", "ServiceManagement", "AVFoundation", "voicestream.sinks.notion",
                   "voicestream.app.native", "PyObjCTools.AppHelper"):
        assert module in hidden
    assert not [m for m in hidden if m.endswith("__main__")]
    for module in ("tkinter", "PyQt6", "PySide6", "webview.platforms.qt", "webview.platforms.gtk"):
        assert module in a.kwargs["excludes"]

    exe = made["EXE"].kwargs
    assert exe["console"] is False and exe["exclude_binaries"] is True  # windowed onedir
    assert exe["target_arch"] == "arm64" and exe["codesign_identity"] is None and exe["argv_emulation"] is False
    assert exe["name"] == "Voicestream"

    bundle = made["BUNDLE"]
    assert bundle.args == (made["COLLECT"],)
    assert bundle.kwargs["name"] == "Voicestream.app"
    assert bundle.kwargs["bundle_identifier"] == info.BUNDLE_ID
    assert bundle.kwargs["version"] == voicestream.__version__
    assert bundle.kwargs["info_plist"] == info.info_plist(voicestream.__version__)


def test_spec_refuses_to_build_without_whisper_server(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="VOICESTREAM_WHISPER_SERVER"):
        _run_spec(monkeypatch, {})
    made = _run_spec(monkeypatch, {"VOICESTREAM_ALLOW_NO_WHISPER": "1"})
    assert not [b for b in made["Analysis"].kwargs["binaries"] if b[1] == "bin"]


# ----------------------------------------------------------------------- icon


def test_make_icon(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    make_icon = _load("make_icon")
    assert make_icon.main(["--out", str(tmp_path)]) == 0
    iconset = tmp_path / "Voicestream.iconset"
    sizes = {p.name: Image.open(p).size for p in iconset.iterdir()}
    assert sizes == {name: (px, px) for name, px in make_icon.ICONSET}
    assert len(sizes) == 10 and sizes["icon_512x512@2x.png"] == (1024, 1024) and sizes["icon_16x16.png"] == (16, 16)
    icns = tmp_path / "Voicestream.icns"
    assert icns.read_bytes()[:4] == b"icns"
    master = Image.open(tmp_path / "Voicestream.png").convert("RGBA")
    assert master.getpixel((4, 4))[3] == 0  # transparent margin around the rounded square
    r, g, b, a = master.getpixel((512, 512))
    assert a == 255 and min(r, g, b) > 240  # the white sound wave in the middle
    r, g, b, _ = master.getpixel((512, 140))
    assert b > r and b > 200  # the blue gradient


# --------------------------------------------------------------- CI speech


SAY_LISTING = """\
Albert              en_US    # Hello! My name is Albert.
Amélie              fr_CA    # Bonjour, je m’appelle Amélie.
Bad News            en_US    # Hello! My name is Bad News.
Daniel              en_GB    # Hello! My name is Daniel.
Eddy (French (France)) fr_FR    # Bonjour, je m’appelle Eddy.
Samantha            en_US    # Hello! My name is Samantha.
Thomas              fr_FR    # Bonjour, je m’appelle Thomas.
Yuna                ko_KR    # 안녕하세요. 제 이름은 유나입니다.
"""


def test_parse_say_voices() -> None:
    voices = make_speech.parse_voices(SAY_LISTING)
    assert ("Eddy (French (France))", "fr_FR") in voices and ("Bad News", "en_US") in voices
    assert len(voices) == 8


def test_pick_voice() -> None:
    voices = make_speech.parse_voices(SAY_LISTING)
    assert make_speech.pick_voice(voices, "en") == "Samantha"
    assert make_speech.pick_voice(voices, "fr") == "Thomas"
    without = [v for v in voices if v[0] not in ("Thomas", "Amélie", "Samantha", "Daniel")]
    assert make_speech.pick_voice(without, "fr") == "Eddy (French (France))"
    assert make_speech.pick_voice(without, "en") is None  # only joke voices left
    assert make_speech.pick_voice(voices, "de") is None


def test_speech_sentences_fit_the_test() -> None:
    for lang, text in make_speech.SENTENCES.items():
        words = [w for w in text.split() if len(w) > 3]
        assert 12 <= len(words) <= 30, lang  # ~6-10 s of speech, enough words for the 60 % overlap check
        assert not any(ch.isdigit() for ch in text)  # whisper may write numbers as digits


# ------------------------------------------------------------- build script, CI


def test_build_script() -> None:
    script = (MAC / "build.sh").read_text(encoding="utf-8")
    for flag in ("-DBUILD_SHARED_LIBS=OFF", "-DGGML_NATIVE=OFF", "-DGGML_OPENMP=OFF", "-DGGML_METAL=ON",
                 "-DGGML_METAL_EMBED_LIBRARY=ON", "-DGGML_BLAS=ON", "-DCMAKE_OSX_ARCHITECTURES=arm64",
                 "-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0", "-DWHISPER_SDL2=OFF", "-DWHISPER_CURL=OFF"):
        assert flag in script
    assert 'WHISPER_CPP_VERSION="${WHISPER_CPP_VERSION:-v1.9.5}"' in script
    assert "--depth 1 --branch" in script and "codesign --verify --deep --strict" in script
    assert "hdiutil create" in script and "ln -s /Applications" in script and "otool -L" in script
    if os.name == "posix" and shutil.which("bash"):
        subprocess.run(["bash", "-n", str(MAC / "build.sh")], check=True)
    if os.name == "posix":
        assert os.access(MAC / "build.sh", os.X_OK)


def test_build_script_prerequisites_match_ci() -> None:
    """The header's install line is what a by-hand build follows: it must bring what CI installs
    (Pillow, for the icon, comes with the dev extra), or the build stops at the icon step."""
    script = (MAC / "build.sh").read_text(encoding="utf-8")
    wf = (ROOT / ".github" / "workflows" / "mac-app.yml").read_text(encoding="utf-8")
    header = re.search(r"pip install -e '\.\[([\w,]+)\]' (pyinstaller==\S+)", script)
    ci = re.search(r'pip install -e "\.\[([\w,]+)\]" (pyinstaller==\S+)', wf)
    assert header and ci
    assert set(header.group(1).split(",")) >= set(ci.group(1).split(",")) and header.group(2) == ci.group(2)
    assert "import PIL" in script  # and says so clearly if it is missing anyway


def test_selftest_launches_without_the_build_trees_whisper_server() -> None:
    script = (MAC / "build.sh").read_text(encoding="utf-8")
    body = script[script.index("step_selftest() {"):script.index("step_dmg() {")]
    assert ('env -u VOICESTREAM_WHISPER_SERVER VOICESTREAM_SELFTEST_UPDATE_DMG="$update_dmg" \\\n'
            '    "$APP/Contents/MacOS/Voicestream" --selftest') in body
    # the update the self-test installs is this app as a newer version, re-signed, in a DMG made here
    assert 'update_dmg="$(make_update_dmg)"' in body and 'rm -rf "$UPDATE_TEST_DIR"' in body
    maker = script[script.index("make_update_dmg() {"):script.index("step_selftest() {")]
    for step in ('plutil -replace CFBundleShortVersionString -string "$UPDATE_TEST_VERSION"',
                 'codesign --force --deep --sign - "$stage/Voicestream.app"', "codesign --verify --deep --strict",
                 'hdiutil create -volname Voicestream -srcfolder "$stage"'):
        assert step in maker
    assert 'check_selftest.py "$SELFTEST_DIR/selftest.json" "$APP"' in body
    # a screenshot at each phase the self-test writes, in the order it writes them (native.py MacShell._selftest)
    phases = [line.split("wait_phase ")[1].split()[0] for line in body.splitlines() if line.strip().startswith("if wait_phase")]
    assert phases == ["window", "floating", "menu"]
    native = (ROOT / "src" / "voicestream" / "app" / "native.py").read_text(encoding="utf-8")
    written = [native.index(f'write_phase(self.report_path, "{p}")') for p in phases]
    assert written == sorted(written)


check_selftest = _load("check_selftest")


def _native_report(app: Path, **changes: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": True, "mode": "native", "frozen": True, "errors": [],
        "whisper_server": str(app / "Contents" / "Frameworks" / "bin" / "whisper-server"),
        "checks": dict.fromkeys(check_selftest.NATIVE_CHECKS, True),
        "quit": {"graceful": True, "shutdown_s": 0.3},
    }
    report.update(changes)
    return report


def test_selftest_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = tmp_path / "dist" / "Voicestream.app"
    assert check_selftest.problems(_native_report(app), app) == []
    # pywebview or a PyObjC framework missing from the bundle: the app falls back to the browser, runs
    # only the HTTP checks, passes them and says ok — still a failure here
    browser = _native_report(app, mode="browser", checks={"app_page": True, "api_app": True})
    found = check_selftest.problems(browser, app)
    assert "fell back to the browser" in found[0] and "status_item" in found[1]
    elsewhere = _native_report(app, whisper_server=str(tmp_path / "build" / "whisper" / "bin" / "whisper-server"))
    assert check_selftest.problems(elsewhere, app) == [
        f"the app did not find its own whisper-server inside Voicestream.app (found: {elsewhere['whisper_server']})"]
    checks = dict.fromkeys(check_selftest.NATIVE_CHECKS, True) | {"app_delegate_patched": False}
    hung = _native_report(app, ok=False, checks=checks, errors=["the app did not quit within 50 s"])
    del hung["quit"]
    assert check_selftest.problems(hung, app) == [
        "failed checks: app_delegate_patched", "error: the app did not quit within 50 s",
        "the app did not finish quitting (its last report, written as it quits, is missing)"]

    # the keyboard shortcut: registered (noErr), a press reaches the host, the menu shows it — and the
    # test of it must not leave the app listening
    for name in ("hotkey_registered", "hotkey_reaches_host", "hotkey_in_menu"):
        assert name in check_selftest.NATIVE_CHECKS
    older = _native_report(app, checks={k: True for k in check_selftest.NATIVE_CHECKS if not k.startswith("hotkey_")})
    assert check_selftest.problems(older, app) == [
        "checks that did not run: hotkey_registered, hotkey_reaches_host, hotkey_in_menu"]
    taken = _native_report(app, ok=False, checks=dict.fromkeys(check_selftest.NATIVE_CHECKS, True) | {"hotkey_registered": False},
                           hotkey={"register_result": -9878, "state_before": "stopped", "state_after": "stopped"})
    assert check_selftest.problems(taken, app) == ["failed checks: hotkey_registered"]
    listening = _native_report(app, hotkey={"state_before": "stopped", "state_after": "listening"})
    assert check_selftest.problems(listening, app) == ["the keyboard shortcut's test left the app listening"]
    # by hand on a Mac that is set up, the app starts listening at launch: still listening is no change
    mine = _native_report(app, hotkey={"state_before": "listening", "state_after": "listening"})
    assert check_selftest.problems(mine, app) == []
    stopped = _native_report(app, hotkey={"state_before": "listening", "state_after": "stopped"})
    assert check_selftest.problems(stopped, app) == ["the keyboard shortcut's test changed the app's state (listening → stopped)"]
    # macOS took it, but what Settings and the menu show never heard of it
    untold = _native_report(app, hotkey={"wanted": True, "register_result": 0, "host_status": "pending",
                                         "state_before": "stopped", "state_after": "stopped"})
    assert check_selftest.problems(untold, app) == [
        "macOS took the keyboard shortcut, but the app's state says 'pending': the menu and Settings don't show it"]
    told = _native_report(app, hotkey={"wanted": True, "register_result": 0, "host_status": "ok"})
    off = _native_report(app, hotkey={"wanted": False, "register_result": 0, "host_status": "off"})  # registered for the test
    assert check_selftest.problems(told, app) == check_selftest.problems(off, app) == []
    # the keyboard layout, read from macOS (Settings labels a shortcut recorded with ⌥ by it)
    assert "keyboard_layout" in check_selftest.NATIVE_CHECKS
    no_layout = _native_report(app, checks={k: True for k in check_selftest.NATIVE_CHECKS if k != "keyboard_layout"})
    assert check_selftest.problems(no_layout, app) == ["checks that did not run: keyboard_layout"]
    # meetings started by hand: the floating button, voicestream:// links, the Spotlight launcher, the microphone
    # probe behind call suggestions — an app that predates them doesn't pass
    new = ("floating_panel", "floating_panel_states", "url_handler", "url_reaches_host", "spotlight_launcher", "mic_probe")
    assert set(new) <= set(check_selftest.NATIVE_CHECKS)
    before = _native_report(app, checks={k: True for k in check_selftest.NATIVE_CHECKS if k not in new})
    assert check_selftest.problems(before, app) == ["checks that did not run: " + ", ".join(new)]
    panel = _native_report(app, ok=False, checks=dict.fromkeys(check_selftest.NATIVE_CHECKS, True) | {"floating_panel": False})
    assert check_selftest.problems(panel, app) == ["failed checks: floating_panel"]
    # the link test asks the host what a keyed toggle would do: it must not start a recording
    recorded = _native_report(app, url={"state_before": "stopped", "state_after": "listening"})
    assert check_selftest.problems(recorded, app) == ["the link test left the app listening"]
    changed = _native_report(app, url={"state_before": "listening", "state_after": "stopped"})
    assert check_selftest.problems(changed, app) == ["the link test changed the app's state (listening → stopped)"]
    assert check_selftest.problems(_native_report(app, url={"state_before": "stopped", "state_after": "stopped"}), app) == []
    # the Meeting menu in the app's menu bar: no unit test sees the real one
    no_menu = _native_report(app, checks={k: True for k in check_selftest.NATIVE_CHECKS if k != "meeting_menu"})
    assert check_selftest.problems(no_menu, app) == ["checks that did not run: meeting_menu"]
    gone = _native_report(app, ok=False, checks=dict.fromkeys(check_selftest.NATIVE_CHECKS, True) | {"meeting_menu": False})
    assert check_selftest.problems(gone, app) == ["failed checks: meeting_menu"]

    report = tmp_path / "selftest.json"
    report.write_text(json.dumps(browser), encoding="utf-8")
    assert check_selftest.main([str(report), str(app)]) == 1
    assert "self-test: FAILED" in capsys.readouterr().out
    report.write_text(json.dumps(_native_report(app)), encoding="utf-8")
    assert check_selftest.main([str(report), str(app)]) == 0
    assert check_selftest.main([str(tmp_path / "missing.json"), str(app)]) == 1


def test_release_job_publishes_tagged_builds_only() -> None:
    wf = (ROOT / ".github" / "workflows" / "mac-app.yml").read_text(encoding="utf-8")
    release = wf[wf.index("\n  release:"):]
    assert "permissions:\n  contents: read" in wf  # everything else stays read-only
    assert "needs: app" in release
    assert "if: startsWith(github.ref, 'refs/tags/v') || startsWith(inputs.release, 'v')" in release
    assert "permissions:\n      contents: write" in release
    assert "actions/download-artifact@v8" in release and "name: ${{ needs.app.outputs.dmg }}" in release
    assert 'gh release create "$TAG" "$DMG" "${verify[@]}" --target "$GITHUB_SHA"' in release
    # a manual run's tag reaches the script through the environment, checked, never as ${{ }} in it
    run = release[release.index("run: |"):]
    assert "${{" not in run and "TAG: ${{ startsWith(github.ref, 'refs/tags/v') && github.ref_name || inputs.release }}" in release
    assert '-${TAG#v}-arm64.dmg' in run  # the tag must be the app's version
    # the release page says how to install an app Apple hasn't notarized, in English and French
    assert "--notes-file packaging/mac/release-notes.md --generate-notes" in release
    assert "sparse-checkout: packaging/mac/release-notes.md" in release
    notes = (ROOT / "packaging" / "mac" / "release-notes.md").read_text(encoding="utf-8")
    assert "**Open Anyway**" in notes and "**Ouvrir quand même**" in notes and "Apple Silicon" in notes
    # the last Open Anyway: the versions after this one install from inside the app (voicestream/app/updater.py)
    assert "install from inside the app" in notes and "s'installent depuis l'app" in notes
    assert "### New in this version" in notes  # the section the update's banner shows (updater.whats_new)
    assert "dmg: ${{ steps.dmg.outputs.name }}" in wf
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://github.com/bybysker/always-on-voice-stream/releases" in readme
    assert "actions/workflows/mac-app.yml?query=is%3Asuccess" in readme


def test_ci_workflow() -> None:
    wf = (ROOT / ".github" / "workflows" / "mac-app.yml").read_text(encoding="utf-8")
    assert "runs-on: macos-15" in wf
    assert "branches: [claude/clever-pascal-hdj7ym]" in wf and 'tags: ["v*"]' in wf and "workflow_dispatch:" in wf
    assert 'python-version: "3.12"' in wf and "pyinstaller==6.22.3" in wf
    assert 'VOICESTREAM_WHISPER_TEST_GPU: "0"' in wf and 'VOICESTREAM_WHISPER_TEST_REQUIRED: "1"' in wf
    assert "continue-on-error: true" in wf  # the Metal run is informational
    assert "ggml-org/whisper-vad" in wf and "ggml-$MODEL.bin" in wf
    for step in ("build.sh whisper", "build.sh check-whisper", "build.sh app", "build.sh verify", "build.sh selftest",
                 "build.sh dmg"):
        assert step in wf
    assert "archive: false" in wf
