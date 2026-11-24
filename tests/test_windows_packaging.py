"""packaging/windows: the PyInstaller spec, the icon, the DLL check and the self-test check.

The spec is executed here with stand-ins for PyInstaller's build classes (as in
test_mac_packaging.py), so what it would hand to PyInstaller is checked without a Windows PC.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from voicestream.config import WhisperCppConfig
from voicestream.transcribe import whisper_cpp

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / "packaging" / "windows"
MAC = ROOT / "packaging" / "mac"


def _load(name: str, folder: Path = WIN) -> Any:
    spec = importlib.util.spec_from_file_location(f"win_{name}", folder / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Target:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs
        self.pure, self.scripts, self.binaries, self.datas = ["pure"], ["scripts"], ["binaries"], ["datas"]


def _run_spec(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> dict[str, Any]:
    hooks = types.SimpleNamespace(
        collect_dynamic_libs=lambda pkg: [(f"/site/{pkg}/lib.dll", pkg)],
        collect_data_files=lambda pkg: [(f"/site/{pkg}/data", pkg)],
        copy_metadata=lambda pkg: [(f"/site/{pkg}-0.dist-info", f"{pkg}-0.dist-info")],
        collect_submodules=lambda pkg, filter=lambda n: True: [  # noqa: A006
            n for n in (pkg, f"{pkg}.__main__", f"{pkg}.app.__main__", f"{pkg}.sinks.notion") if filter(n)
        ],
    )
    monkeypatch.setitem(sys.modules, "PyInstaller", types.ModuleType("PyInstaller"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", types.ModuleType("PyInstaller.utils"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    monkeypatch.setitem(sys.modules, "bundle_info", _load("bundle_info", MAC))
    monkeypatch.setattr(sys, "path", list(sys.path))
    for key in ("VOICESTREAM_WHISPER_SERVER", "VOICESTREAM_ALLOW_NO_WHISPER", "VOICESTREAM_BUNDLE_AGENT_CLI"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    made: dict[str, Any] = {}

    def recorder(kind: str) -> Any:
        def make(*args: Any, **kwargs: Any) -> Target:
            made[kind] = t = Target(*args, **kwargs)
            return t

        return make

    namespace = {"SPECPATH": str(WIN), "__name__": "__main__"}
    namespace.update({k: recorder(k) for k in ("Analysis", "PYZ", "EXE", "COLLECT")})
    exec(compile((WIN / "voicestream.spec").read_text(encoding="utf-8"), str(WIN / "voicestream.spec"), "exec"), namespace)  # noqa: S102
    return made


def test_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = tmp_path / "whisper-server.exe"
    ws.write_bytes(b"MZ")
    ws.chmod(ws.stat().st_mode | stat.S_IXUSR)
    made = _run_spec(monkeypatch, {"VOICESTREAM_WHISPER_SERVER": str(ws)})
    a = made["Analysis"]
    assert a.args[0][0].endswith(str(Path("voicestream") / "app" / "__main__.py"))
    assert (str(ws.resolve()), "bin") in a.kwargs["binaries"]  # _internal/bin, where find_binary looks
    hidden = a.kwargs["hiddenimports"]
    assert "voicestream.sinks.notion" in hidden and "voicestream.__main__" not in hidden
    assert "keyring.backends.Windows" in hidden  # the keys in the Credential Manager
    assert "pystray._win32" in hidden  # the icon next to the clock: pystray imports its backend by name
    assert any(src.startswith("/site/soundcard/") for src, _ in a.kwargs["datas"])  # WASAPI loopback's .h files
    assert {"webview", "objc", "catap"} <= set(a.kwargs["excludes"])
    exe = made["EXE"].kwargs
    assert exe["name"] == "Voicestream" and exe["console"] is False and exe["exclude_binaries"] is True
    assert made["COLLECT"].kwargs["name"] == "Voicestream"
    assert "BUNDLE" not in made


def test_spec_needs_a_whisper_server(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="VOICESTREAM_WHISPER_SERVER"):
        _run_spec(monkeypatch, {})
    made = _run_spec(monkeypatch, {"VOICESTREAM_ALLOW_NO_WHISPER": "1"})
    assert not any(dest == "bin" for _, dest in made["Analysis"].kwargs["binaries"])


def test_icon(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    assert _load("make_icon").main(["--out", str(tmp_path)]) == 0
    with Image.open(tmp_path / "Voicestream.ico") as ico:
        assert ico.format == "ICO"
        assert (256, 256) in ico.info["sizes"] and (16, 16) in ico.info["sizes"]


def test_dll_check() -> None:
    pytest.importorskip("pefile")
    check = _load("check_exe")
    assert check.foreign(["kernel32.dll", "ws2_32.dll", "api-ms-win-core-synch-l1-2-0.dll"]) == []
    assert check.foreign(["kernel32.dll", "vcruntime140.dll", "msvcp140.dll"]) == ["vcruntime140.dll", "msvcp140.dll"]


def test_selftest_check(tmp_path: Path) -> None:
    check = _load("check_selftest")
    good = {
        "ok": True, "mode": "tray", "frozen": True, "errors": [], "quit": {"graceful": True},
        "checks": {"app_page": True, "api_app": True, "whisper_server_bundled": True, "tray_icon": True,
                   "tray_menu": True},
    }
    assert check.problems(good) == []
    bad = {**good, "ok": False, "checks": {**good["checks"], "whisper_server_bundled": False}, "quit": {}}
    assert check.problems(bad) == [
        "check failed: whisper_server_bundled", "the report is not ok", "the app did not stop the graceful way",
    ]
    assert check.problems({**good, "mode": "headless"})[0].startswith("the app ran in 'headless' mode")
    report = tmp_path / "selftest.json"
    report.write_text(json.dumps(good), encoding="utf-8")
    assert check.main([str(report)]) == 0
    assert check.main([str(tmp_path / "missing.json")]) == 1
    second = tmp_path / "second.json"
    second.write_text(json.dumps({"ok": True, "mode": "already-running", "found_first": True}), encoding="utf-8")
    assert check.main([str(report), str(second)]) == 0
    second.write_text(json.dumps({**good}), encoding="utf-8")  # a second copy that started a second app
    assert check.main([str(report), str(second)]) == 1
    assert check.main([str(report), str(tmp_path / "none.json")]) == 1


def test_find_binary_tries_exe_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bundled copy is whisper-server.exe: the name without a suffix finds it on Windows."""
    exe = tmp_path / "whisper-server.exe"
    exe.write_text("")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.delenv(whisper_cpp.BINARY_ENV, raising=False)
    monkeypatch.setattr(whisper_cpp, "HOMEBREW_PATHS", ())
    monkeypatch.setattr(whisper_cpp.shutil, "which", lambda name: None)
    cfg = WhisperCppConfig(binary=str(tmp_path / "whisper-server"))
    monkeypatch.setattr(sys, "platform", "linux")
    assert whisper_cpp.find_binary(cfg) is None
    monkeypatch.setattr(sys, "platform", "win32")
    assert whisper_cpp.find_binary(cfg) == exe
