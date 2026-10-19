# PyInstaller spec for Voicestream.app (macOS, Apple Silicon, onedir, windowed).
#
# Build it with packaging/mac/build.sh (which builds whisper-server and the icon first), or:
#   VOICESTREAM_WHISPER_SERVER=/path/to/whisper-server \
#     python -m PyInstaller --noconfirm --distpath dist --workpath build/pyinstaller packaging/mac/voicestream.spec
#
# Pinned with the build: PyInstaller 6.22.3, Python 3.12. The .app is ad-hoc signed (PyInstaller's
# default without a codesign identity, no hardened runtime, so no entitlements are needed).
# Info.plist keys live in bundle_info.py, where tests can check them.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

HERE = Path(SPECPATH).resolve()  # noqa: F821 - defined by PyInstaller
sys.path.insert(0, str(HERE))
import bundle_info as info  # noqa: E402

ROOT = info.ROOT
VERSION = info.package_version()

# catap loads its Core Audio helper with ctypes (catap/native/libcatap_coreaudio.dylib): invisible to analysis
binaries = collect_dynamic_libs("catap")
whisper = info.whisper_server()
if whisper is not None:
    # -> Contents/Frameworks/bin/whisper-server, where transcribe/whisper_cpp.find_binary looks (sys._MEIPASS/bin)
    binaries.append((str(whisper), "bin"))
agent_cli = info.agent_cli()
if agent_cli is not None:
    binaries.append((str(agent_cli), "claude_agent_sdk/_bundled"))  # where the SDK looks for its CLI

# static UI, config template, Apps Script bridge...; plus voicestream's own metadata
datas = collect_data_files("voicestream") + copy_metadata("voicestream")

hiddenimports = [
    # sinks, transcription backends and app modules are imported by name at run time
    *collect_submodules("voicestream", filter=lambda name: not name.endswith("__main__")),
    "webview.platforms.cocoa",
    "objc",
    "Foundation",
    "AppKit",
    "WebKit",
    "Quartz",
    "AVFoundation",
    "ServiceManagement",
    "CoreAudio",
    "PyObjCTools.AppHelper",
    "keyring.backends.macOS",
]

excludes = [
    "tkinter",
    "_tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "qtpy",
    "gi",
    "cefpython3",
    "IPython",
    "matplotlib",
    "pytest",
    "_pytest",
    # pywebview's other GUI backends (its guilib imports each one in turn)
    "webview.platforms.qt",
    "webview.platforms.gtk",
    "webview.platforms.cef",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.mshtml",
    "webview.platforms.win32",
    "webview.platforms.android",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "src" / "voicestream" / "app" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir: one-file .app bundles are deprecated (blocked in PyInstaller 7)
    name=info.APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,  # ad-hoc
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=info.APP_NAME,
)

icon = info.icon()
app = BUNDLE(  # noqa: F821
    coll,
    name=f"{info.APP_NAME}.app",
    icon=str(icon) if icon else None,
    bundle_identifier=info.BUNDLE_ID,
    version=VERSION,
    info_plist=info.info_plist(VERSION),
)
