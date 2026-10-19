"""Draw Voicestream's app icon and export it as ``Voicestream.iconset`` + ``Voicestream.icns``.

The icon is generated at build time rather than kept in the repository as a binary: a
rounded square with a blue-violet gradient and a white sound wave (seven rounded bars).
``iconutil`` (macOS) turns the iconset into the .icns; elsewhere Pillow writes it, so the
script also runs in Linux tests. The menu-bar icon needs no files: it uses SF Symbols.

    python packaging/mac/make_icon.py [--out packaging/mac/build]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

NAME = "Voicestream"
CANVAS = 1024
SUPERSAMPLE = 2  # draw at 2048 px and scale down: Pillow's shapes have no anti-aliasing
TOP, BOTTOM = (99, 102, 241), (14, 165, 233)  # indigo -> sky blue
BARS = (0.30, 0.52, 0.78, 1.00, 0.78, 0.52, 0.30)  # relative heights of the sound-wave bars

# (file name in the .iconset, pixel size): what iconutil expects
ICONSET = [
    (f"icon_{s}x{s}{'@2x' if scale == 2 else ''}.png", s * scale)
    for s in (16, 32, 128, 256, 512)
    for scale in (1, 2)
]


def _gradient(size: int) -> Image.Image:
    """Vertical gradient, top colour to bottom colour."""
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM, strict=True)))
    return column.resize((size, size))


def draw(size: int = CANVAS) -> Image.Image:
    """The icon at ``size`` px, with macOS's standard margin around the rounded square."""
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    # Apple's grid: an 824/1024 rounded square, corner radius ~22.5% of it, centred
    inset = round(big * 100 / 1024)
    side = big - 2 * inset
    radius = round(side * 0.225)
    box = (inset, inset, inset + side, inset + side)

    shadow = Image.new("L", (big, big), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((box[0], box[1] + big // 64, box[2], box[3] + big // 64), radius, fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(big // 80))
    img.paste((0, 0, 0, 255), (0, 0), shadow)

    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    tile.paste(_gradient(side), (inset, inset))
    img.paste(tile, (0, 0), mask)

    pen = ImageDraw.Draw(img)
    bar_w = side * 0.072
    gap = side * 0.044
    total = len(BARS) * bar_w + (len(BARS) - 1) * gap
    x = big / 2 - total / 2
    max_h = side * 0.50
    for rel in BARS:
        h = max_h * rel
        top = big / 2 - h / 2
        pen.rounded_rectangle((x, top, x + bar_w, top + h), radius=bar_w / 2, fill=(255, 255, 255, 255))
        x += bar_w + gap
    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_iconset(master: Image.Image, iconset: Path) -> list[Path]:
    iconset.mkdir(parents=True, exist_ok=True)
    written = []
    for name, px in ICONSET:
        path = iconset / name
        master.resize((px, px), Image.Resampling.LANCZOS).save(path, format="PNG")
        written.append(path)
    return written


def write_icns(master: Image.Image, iconset: Path, icns: Path) -> str:
    """``iconutil`` when there is one (macOS), else Pillow's ICNS writer. Returns which one."""
    iconutil = shutil.which("iconutil")
    if iconutil:
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(icns)], check=True)  # noqa: S603
        return "iconutil"
    sizes = sorted({px for _, px in ICONSET})
    master.save(icns, format="ICNS", append_images=[master.resize((s, s), Image.Resampling.LANCZOS) for s in sizes])
    return "pillow"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "build", help="output folder")
    args = p.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    master = draw(CANVAS)
    master.save(args.out / f"{NAME}.png")
    iconset = args.out / f"{NAME}.iconset"
    write_iconset(master, iconset)
    how = write_icns(master, iconset, args.out / f"{NAME}.icns")
    print(f"{args.out / f'{NAME}.icns'} ({how})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
