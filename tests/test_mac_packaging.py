"""packaging/mac: the Info.plist, the PyInstaller spec, the icon, the CI speech and build scripts.

The spec is executed here with stand-ins for PyInstaller's build classes, so what it would hand
to PyInstaller (binaries, data, hidden imports, EXE/BUNDLE options) is checked without a Mac.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
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
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(MAC / "build.sh")], check=True)
    if os.name == "posix":
        assert os.access(MAC / "build.sh", os.X_OK)


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
