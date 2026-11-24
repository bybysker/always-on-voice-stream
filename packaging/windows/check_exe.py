"""Fail unless an .exe needs only DLLs that every Windows 10/11 has.

whisper-server.exe sits in the app's ``_internal/bin``, where Windows doesn't look for the DLLs that
PyInstaller puts in ``_internal``: it must be built with the static C runtime (no VCRUNTIME140.dll,
no MSVCP140.dll), or it would start only on PCs that have the Visual C++ Redistributable.

    python packaging/windows/check_exe.py build/whisper/bin/whisper-server.exe
"""

from __future__ import annotations

import sys

import pefile  # a PyInstaller dependency on Windows

SYSTEM = {
    "kernel32.dll", "user32.dll", "advapi32.dll", "shell32.dll", "ole32.dll", "oleaut32.dll",
    "ws2_32.dll", "bcrypt.dll", "ntdll.dll", "shlwapi.dll", "gdi32.dll", "winmm.dll", "dbghelp.dll",
    "crypt32.dll", "secur32.dll", "iphlpapi.dll", "psapi.dll", "version.dll", "userenv.dll",
}


def imports(path: str) -> list[str]:
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    return sorted({e.dll.decode().lower() for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])})


def foreign(dlls: list[str]) -> list[str]:
    """What isn't Windows' own (the api-ms-win-core-* sets are; api-ms-win-crt-* is the UCRT, which is
    there since Windows 10 too)."""
    return [d for d in dlls if d not in SYSTEM and not d.startswith("api-ms-win-")]


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        dlls = imports(path)
        print(f"{path}: {', '.join(dlls)}")
        bad = foreign(dlls)
        if bad:
            print(f"error: {path} needs DLLs a Windows PC may not have: {', '.join(bad)}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
