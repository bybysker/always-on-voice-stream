"""Check the Windows app's self-test report (packaging/windows/build.sh selftest).

    python packaging/windows/check_selftest.py build/selftest-windows/selftest.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED = ("app_page", "api_app", "whisper_server_bundled")


def problems(report: dict) -> list[str]:
    out = []
    if not report.get("frozen"):
        out.append("the report doesn't come from the packaged app")
    checks = report.get("checks") or {}
    out += [f"check failed: {name}" for name in REQUIRED if not checks.get(name)]
    out += [f"error: {e}" for e in report.get("errors") or []]
    if not report.get("ok"):
        out.append("the report is not ok")
    quit_ = report.get("quit") or {}
    if not quit_.get("graceful"):
        out.append("the app did not stop the graceful way")
    return out


def main(argv: list[str]) -> int:
    path = Path(argv[0] if argv else "build/selftest-windows/selftest.json")
    if not path.is_file():
        print(f"error: no self-test report at {path}", file=sys.stderr)
        return 1
    report = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({k: report.get(k) for k in ("ok", "mode", "version", "platform", "checks", "quit")}, indent=2))
    found = problems(report)
    for p in found:
        print(p, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
