"""Draw Voicestream's icon (the Mac app's drawing, packaging/mac/make_icon.py) as a Windows .ico.

    python packaging/windows/make_icon.py [--out packaging/windows/build]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "mac"))
import make_icon as mac  # noqa: E402

SIZES = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=HERE / "build", help="output folder")
    args = p.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    ico = args.out / f"{mac.NAME}.ico"
    mac.draw().save(ico, format="ICO", sizes=SIZES)
    print(ico)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
