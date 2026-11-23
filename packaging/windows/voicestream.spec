# PyInstaller spec for the Windows app (x64, onedir): dist/Voicestream/Voicestream.exe.
#
# Build it with packaging/windows/build.sh (which builds whisper-server.exe and the icon first), or:
#   VOICESTREAM_WHISPER_SERVER=C:/path/to/whisper-server.exe \
#     python -m PyInstaller --noconfirm --distpath dist --workpath build/pyinstaller packaging/windows/voicestream.spec
#
# The app opens its UI in the default browser (launcher.py "browser" mode: the Mac window and menu-bar
# icon are Cocoa only). It keeps a console window: it shows the app's address, and Ctrl+C in it stops
# the app the graceful way (a meeting in progress is wrapped up and saved).
# Pinned with the build: PyInstaller 6.22.3, Python 3.12. Not code-signed: Windows SmartScreen warns once.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

HERE = Path(SPECPATH).resolve()  # noqa: F821 - defined by PyInstaller
sys.path.insert(0, str(HERE.parent / "mac"))
import bundle_info as info  # noqa: E402 - the version and the whisper-server to bundle, shared with the Mac app

ROOT = info.ROOT

binaries = collect_dynamic_libs("onnxruntime")  # its native library, loaded by its own Python module
binaries += collect_dynamic_libs("_sounddevice_data")  # PortAudio's DLL, which sounddevice loads with ctypes
whisper = info.whisper_server()
if whisper is not None:
    # -> _internal/bin/whisper-server.exe, where transcribe/whisper_cpp.find_binary looks (sys._MEIPASS/bin)
    binaries.append((str(whisper), "bin"))

# static UI, config template, Apps Script bridge...; voicestream's own metadata;
# soundcard's C declarations (it reads its .h files at import for cffi: WASAPI loopback)
datas = collect_data_files("voicestream") + copy_metadata("voicestream") + collect_data_files("soundcard")

hiddenimports = [
    # sinks, transcription backends and app modules are imported by name at run time
    *collect_submodules("voicestream", filter=lambda name: not name.endswith("__main__")),
    "keyring.backends.Windows",  # API keys in the Windows Credential Manager
    *collect_submodules("win32ctypes"),
    "soundcard.mediafoundation",
    "onnxruntime",  # voices.py imports it when it tells the people on the meeting audio apart
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
    "webview",  # the Mac shell only (voicestream/app/native.py)
    "objc",
    "AppKit",
    "catap",
]

icon = HERE / "build" / f"{info.APP_NAME}.ico"

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
    exclude_binaries=True,  # onedir: starts fast, nothing unpacked to %TEMP% at each launch
    name=info.APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(icon) if icon.is_file() else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=info.APP_NAME,
)
