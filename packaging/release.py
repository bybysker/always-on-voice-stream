"""Get a release ready: check the new version, then write it where the app reads it.

Run by the release workflow (.github/workflows/release.yml: Actions → release → Run workflow), which
then commits it to main, tags it and starts the Mac build that tests and publishes it (mac-app.yml).
Every installed Voicestream offers a published release within six hours (updater.CHECK_EVERY_S).

Everything is checked before anything changes, so a mistake costs a few seconds, not a Mac build:

* the version is one the app reads (:func:`voicestream.app.updater.parse_version`) and the release job
  accepts (:data:`TAG`);
* it is newer than this one, as the app compares them: an older one would never be offered;
* it isn't released already (no tag of that name);
* the release notes (packaging/mac/release-notes.md) were written for it: their first line names the
  version (``<!-- release notes for 0.4.0 -->``). Their "New in this version" is what the update's
  banner shows, so a release never goes out with the last one's.

The version is then written in pyproject.toml and src/voicestream/__init__.py (the app's own version,
and the DMG's name). Prints ``version=…``, ``tag=…`` and ``prerelease=…`` lines (for $GITHUB_OUTPUT).

    python packaging/release.py 0.4.0 --check     # only the checks, on your own computer
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voicestream.app.updater import NOTES_MAX, Version, parse_version, whats_new  # noqa: E402

TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$")
"""What mac-app.yml's release job accepts as a release tag (the same pattern)."""
NOTES = Path("packaging/mac/release-notes.md")
NOTES_FOR = re.compile(r"\A<!--\s*release notes for v?(\S+)\s*-->")
PYPROJECT = Path("pyproject.toml")
INIT = Path("src/voicestream/__init__.py")
_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_INIT_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: Version
    tag: str

    @property
    def prerelease(self) -> bool:
        return "-" in self.tag  # what the release job marks as a pre-release on GitHub


def current(root: Path) -> Version:
    """The version in the source now (pyproject.toml and __init__.py must agree)."""
    found = [pattern.search((root / path).read_text(encoding="utf-8"))
             for path, pattern in ((PYPROJECT, _PYPROJECT_VERSION), (INIT, _INIT_VERSION))]
    texts = [m.group(1) if m else "(none)" for m in found]
    if texts[0] != texts[1]:
        raise ReleaseError(f"{PYPROJECT} says {texts[0]} and {INIT} {texts[1]}: they must say the same version")
    version = parse_version(texts[0])
    if version is None:
        raise ReleaseError("the current version isn't one the app reads")
    return version


def notes_version(root: Path) -> str | None:
    m = NOTES_FOR.match((root / NOTES).read_text(encoding="utf-8"))
    return m.group(1) if m else None


def check(root: Path, wanted: str, tags: Iterable[str]) -> Release:
    """The release of ``wanted`` ("0.4.0" or "v0.4.0"), or :class:`ReleaseError` saying what is wrong."""
    text = wanted.strip().removeprefix("v")
    version, tag = parse_version(text), f"v{text}"
    if version is None or not TAG.match(tag):
        raise ReleaseError(f"{wanted!r} isn't a version: write it like 0.4.0, or 0.5.0-beta.1 for a pre-release")
    now = current(root)
    if not version > now:
        raise ReleaseError(f"{text} isn't newer than {now}: the app would never offer it")
    if tag in set(tags):
        raise ReleaseError(f"{tag} is released already")
    notes = notes_version(root)
    if notes is None:
        raise ReleaseError(f"{NOTES} must start with <!-- release notes for {text} --> (and say what is new in it)")
    if parse_version(notes) != version:
        raise ReleaseError(f"{NOTES} are the notes for {notes}: write the ones for {text} first, and its first line "
                           f"<!-- release notes for {text} --> (their \"New in this version\" is the update's banner)")
    new = whats_new((root / NOTES).read_text(encoding="utf-8"))
    if not new:
        raise ReleaseError(f"{NOTES} have no \"### New in this version\" section: the update's banner shows it")
    if new.endswith("\n…"):  # whats_new cut it there
        raise ReleaseError(f"\"New in this version\" is longer than the banner shows ({NOTES_MAX} characters): shorten it")
    return Release(version, tag)


def write(root: Path, release: Release) -> None:
    """Put the version where the app reads it (pyproject.toml, __init__.py)."""
    text = str(release.version)
    for path, pattern in ((PYPROJECT, _PYPROJECT_VERSION), (INIT, _INIT_VERSION)):
        file = root / path
        old = file.read_text(encoding="utf-8")
        m = pattern.search(old)
        assert m is not None, path  # current() found it
        file.write_text(old[: m.start(1)] + text + old[m.end(1) :], encoding="utf-8")


def git_tags(root: Path) -> list[str]:
    out = subprocess.run(["git", "tag", "--list"], cwd=root, capture_output=True, text=True, check=True)
    return out.stdout.split()


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="Check a new version and write it where the app reads it.")
    parser.add_argument("version", help="e.g. 0.4.0, or 0.5.0-beta.1 for a pre-release")
    parser.add_argument("--check", action="store_true", help="only the checks: change nothing")
    args = parser.parse_args(argv)
    try:
        release = check(root, args.version, git_tags(root))
    except ReleaseError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if not args.check:
        write(root, release)
    print(f"version={release.version}\ntag={release.tag}\nprerelease={str(release.prerelease).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
