"""In-app updates: find a newer Voicestream on GitHub, check it, and put it in place of this one.

Until 0.3 each version meant downloading the DMG, dragging the app to Applications and going through
Gatekeeper's "Open Anyway" again. From here on the app does it, in four steps:

1. **Check.** GitHub's releases API for ``bybysker/always-on-voice-stream`` (no key: 60 requests an hour
   per address is plenty for one look every six hours), with the ETag of the last answer so an
   unchanged release costs nothing, a short timeout, and silence when the Mac is offline. Drafts are
   never offered, pre-releases only when ``prefs.update_prereleases`` says so. Versions are compared
   as PEP 440 orders them (``0.4.0rc1`` < ``0.4.0`` < ``0.4.0.post1``; semver's ``0.4.0-beta.1`` too).
2. **Download.** The release's ``Voicestream-<version>-arm64.dmg`` into the app's support folder,
   refused unless the API gives its SHA-256 (``"digest": "sha256:…"``) and the file has exactly that
   size and that digest. The API is reached over TLS at api.github.com and the file only from GitHub's
   own hosts, so a file that matches is the one published with the release.
3. **Check the app inside.** The DMG mounted read-only and hidden from Finder; its Voicestream.app must
   have our bundle id, the release's version, a signature ``codesign --verify --deep --strict`` accepts
   (ad-hoc: it proves the bundle is whole, not who made it — the digest above does that) and arm64 code.
   It is copied with ``ditto`` next to the running app (the same volume, so moving it in is a rename).
4. **Swap and relaunch.** A small ``/bin/sh`` helper (:data:`HELPER`), written to the support folder and
   started in its own session so it outlives the app, waits for the app to quit, moves the old bundle
   aside, the new one in, and only then removes the old one; on any failure the old one goes back. It
   drops any quarantine attribute and opens the new version.

Why no Gatekeeper prompt this time: macOS adds ``com.apple.quarantine`` to a downloaded file only when
the app that wrote it asks for it (``LSFileQuarantineEnabled`` in its Info.plist) or is sandboxed.
Voicestream is neither, and downloads with Python's own HTTP code, so the DMG, the app copied from it
(with ``ditto --noqtn`` for good measure) and the bundle moved into place carry no quarantine, and
Gatekeeper only assesses quarantined apps on first launch. The self-test checks the DMG has none.

macOS 13's "App Management" protection may stop one app from changing another app's bundle unless both
are signed by the same team (WWDC22 "What's new in privacy"). An ad-hoc signature has no team, so
whether it applies to Voicestream replacing itself isn't documented: before quitting, the app tries a
harmless write inside its own bundle (:func:`app_management_blocked`); if macOS refuses it, the update
isn't attempted and the verified DMG is opened in Finder with a short instruction instead. The helper
also falls back to that if the move itself is refused, after putting the old app back: you are never
left without a working Voicestream.

Nothing here imports AppKit: the host runs it on its loop (blocking steps in worker threads), and the
pure parts (versions, releases, digests, the state machine, the helper's text) are tested on Linux.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import http.server
import json
import logging
import os
import plistlib
import re
import shutil
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .. import __version__

log = logging.getLogger(__name__)

REPO = "bybysker/always-on-voice-stream"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
"""The newest release that is neither a draft nor a pre-release (GitHub picks it)."""
LIST_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
"""Every recent release, for those who want pre-releases too (drafts are only listed to the owner)."""
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
APP_NAME = "Voicestream"
BUNDLE_ID = "io.github.bybysker.voicestream"
"""As packaging/mac/bundle_info.py sets it: an app with another id is not an update of this one."""
CHECK_EVERY_S = 6 * 3600.0
FIRST_CHECK_DELAY_S = 20.0
"""The check at launch waits this long: opening the app comes first."""
HTTP_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 60.0
"""A download that receives nothing for this long has stalled."""
MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_DMG_BYTES = 2 * 1024**3
CHUNK = 1024 * 1024
NOTES_MAX = 1500
HELPER_WAIT_S = 600
"""The helper waits this long for the app to quit, then gives up (the quit was cancelled)."""
USER_AGENT = f"Voicestream/{__version__} (+{RELEASES_PAGE})"  # GitHub's API refuses requests without one
ARM64 = 0x0100000C  # mach/machine.h CPU_TYPE_ARM64 = CPU_TYPE_ARM | CPU_ARCH_ABI64
X86_64 = 0x01000007

Phase = Literal["idle", "available", "downloading", "ready", "installing", "failed"]


class UpdateError(RuntimeError):
    """A step of the update could not be done: the message for the log, and a ``code`` the window says in
    your language (``upd.err.<code>`` in app.js)."""

    def __init__(self, message: str, code: str = "unexpected") -> None:
        super().__init__(message)
        self.code = code


class AppManagementBlocked(UpdateError):
    """macOS refuses changes to the app's bundle (App Management): the update is installed by hand instead."""


# ------------------------------------------------------------------------------ versions


_VERSION = re.compile(
    r"""^\s*v?(?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre>alpha|beta|preview|pre|rc|a|b|c)[-_.]?(?P<pre_n>\d+)?)?
    (?:[-_.]?(?:post|rev|r)[-_.]?(?P<post>\d+))?
    (?P<devtag>[-_.]?dev[-_.]?(?P<dev>\d+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


@dataclass(frozen=True, order=False)
class Version:
    """A version as PEP 440 orders it (semver's ``-beta.1`` reads as ``b1``); compare with ``<``, ``==``."""

    text: str
    release: tuple[int, ...]
    pre: tuple[int, int] | None = None
    post: int | None = None
    dev: int | None = None

    @property
    def key(self) -> tuple[Any, ...]:
        release = self.release
        while len(release) > 1 and release[-1] == 0:
            release = release[:-1]  # 0.4 == 0.4.0
        if self.pre is None and self.post is None and self.dev is not None:
            pre: tuple[float, float] = (-1, 0)  # 0.4.0.dev1 comes before 0.4.0a1
        elif self.pre is None:
            pre = (float("inf"), 0)
        else:
            pre = self.pre
        return (release, pre, -1 if self.post is None else self.post, float("inf") if self.dev is None else self.dev)

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    def __lt__(self, other: Version) -> bool:
        return self.key < other.key

    def __le__(self, other: Version) -> bool:
        return self.key <= other.key

    def __gt__(self, other: Version) -> bool:
        return self.key > other.key

    def __ge__(self, other: Version) -> bool:
        return self.key >= other.key

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        return self.text


def parse_version(text: Any) -> Version | None:
    """``"v0.4.0"`` → a :class:`Version` (``text`` without the ``v``); ``None`` for anything else."""
    m = _VERSION.match(str(text or ""))
    if m is None:
        return None
    pre = None
    if m["pre"]:
        pre = (_PRE_RANK[m["pre"].lower()], int(m["pre_n"] or 0))
    return Version(
        text=str(text).strip().removeprefix("v").removeprefix("V"),
        release=tuple(int(p) for p in m["release"].split(".")),
        pre=pre,
        post=int(m["post"]) if m["post"] else None,
        dev=(int(m["dev"] or 0)) if m["devtag"] else None,
    )


def current_version() -> Version:
    v = parse_version(__version__)
    assert v is not None, __version__
    return v


# ------------------------------------------------------------------------------ releases


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None
    """Lowercase hex, from the API's ``"digest": "sha256:…"``; ``None``: GitHub gave none, the file can't be checked."""


@dataclass(frozen=True)
class Release:
    version: Version
    tag: str
    name: str
    notes: str
    """The release notes' "New in this version" section as plain text (:func:`whats_new`)."""
    url: str
    published_at: str | None
    prerelease: bool
    asset: Asset | None

    def as_dict(self) -> dict[str, Any]:
        a = self.asset
        return {
            "version": str(self.version), "tag": self.tag, "name": self.name, "notes": self.notes, "url": self.url,
            "published_at": self.published_at, "prerelease": self.prerelease,
            "asset": {"name": a.name, "url": a.url, "size": a.size, "sha256": a.sha256} if a else None,
        }  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Release | None:
        """What :meth:`as_dict` saved (the cached answer behind an ETag); ``None`` if it doesn't read back."""
        try:
            v = parse_version(d["version"])
            a = d.get("asset")
            asset = Asset(str(a["name"]), str(a["url"]), int(a["size"]), parse_digest(f"sha256:{a['sha256']}")
                          if a.get("sha256") else None) if a else None
            if v is None:
                return None
            return cls(v, str(d["tag"]), str(d.get("name") or ""), str(d.get("notes") or ""), str(d.get("url") or ""),
                       d.get("published_at"), bool(d.get("prerelease")), asset)
        except (KeyError, TypeError, ValueError):
            return None


def asset_name(version: Version | str) -> str:
    """The DMG mac-app.yml attaches to a release: ``Voicestream-0.4.0-arm64.dmg`` for tag ``v0.4.0``."""
    return f"{APP_NAME}-{version}-arm64.dmg"


def parse_digest(value: Any) -> str | None:
    """``"sha256:<64 hex>"`` → the hex, lowercase; ``None`` for anything else (another algorithm, no digest)."""
    m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", str(value or "").strip())
    return m.group(1).lower() if m else None


def parse_release(data: Any) -> Release | None:
    """One release from GitHub's API, or ``None``: a draft, a tag that isn't a version, not an object."""
    if not isinstance(data, dict) or data.get("draft"):
        return None
    tag = str(data.get("tag_name") or "")
    version = parse_version(tag)
    if version is None:
        return None
    wanted = asset_name(version)
    asset = None
    for a in data.get("assets") or []:
        if isinstance(a, dict) and a.get("name") == wanted:
            url, size = a.get("browser_download_url"), a.get("size")
            if isinstance(url, str) and isinstance(size, int) and size > 0:
                asset = Asset(wanted, url, size, parse_digest(a.get("digest")))
            break
    return Release(
        version=version, tag=tag, name=str(data.get("name") or f"{APP_NAME} {tag}")[:200],
        notes=whats_new(str(data.get("body") or "")), url=str(data.get("html_url") or RELEASES_PAGE),
        published_at=data.get("published_at") if isinstance(data.get("published_at"), str) else None,
        prerelease=bool(data.get("prerelease")) or version.is_prerelease, asset=asset,
    )  # fmt: skip


def pick_release(data: Any, *, prereleases: bool = False) -> Release | None:
    """The newest release in an answer of the API (one release, or a list of them), drafts never,
    pre-releases only with ``prereleases``."""
    items = data if isinstance(data, list) else [data]
    found = [r for r in map(parse_release, items) if r is not None and (prereleases or not r.prerelease)]
    return max(found, key=lambda r: r.version.key, default=None)


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def whats_new(body: str) -> str:
    """The "New in this version" section of release notes (packaging/mac/release-notes.md), as plain text:
    no markup left (links become their words, emphasis goes, list items start with "• "), at most
    :data:`NOTES_MAX` characters. Empty when the notes have no such section. The page shows it as text,
    never as HTML: it comes from the internet."""
    body = re.sub(r"<!--.*?-->", "", body.replace("\r\n", "\n"), flags=re.DOTALL)
    out: list[str] = []
    level = 0
    for line in body.split("\n"):
        m = _HEADING.match(line)
        if level:
            if m and len(m.group(1)) <= level:
                break
            out.append(line)
        elif m and re.fullmatch(r"(what'?s\s+)?new\s+in\s+this\s+version|nouveaut[ée]s(\s+de\s+cette\s+version)?",
                                m.group(2).strip().lower()):
            level = len(m.group(1))
    lines = []
    for line in out:
        text = _LINK.sub(r"\1", line.rstrip())
        text = re.sub(r"(\*\*|__|`)", "", text)
        text = re.sub(r"(?<![\w*])[*_]([^*_\n]+)[*_](?![\w*])", r"\1", text)
        text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "• ", text)
        text = re.sub(r"^#{1,6}\s+", "", text)
        text = "".join(c for c in text if c.isprintable() or c == "\t")
        text = text.strip()
        if text and lines and lines[-1] and not text.startswith("• "):
            lines[-1] += " " + text  # a line that goes on a paragraph or a list item, as Markdown reads it
        elif text or (lines and lines[-1]):
            lines.append(text)
    text = "\n".join(lines).strip()
    if len(text) > NOTES_MAX:
        text = text[: text.rfind("\n", 0, NOTES_MAX) if "\n" in text[:NOTES_MAX] else NOTES_MAX].rstrip() + "\n…"
    return text


# ------------------------------------------------------------------------------- the feed


def allowed_url(url: str, *, local: bool = False) -> bool:
    """Where the app may fetch release data and DMGs from: GitHub over HTTPS (the download redirects to
    ``*.githubusercontent.com``). ``local``: also http://127.0.0.1 (the self-test's server, nothing else)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if local and parts.scheme == "http" and host in ("127.0.0.1", "localhost"):
        return True
    return parts.scheme == "https" and (host in ("github.com", "api.github.com") or host.endswith(".githubusercontent.com"))


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to where :func:`allowed_url` allows (GitHub's file hosts), never to plain HTTP."""

    def __init__(self, local: bool) -> None:
        self.local = local

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if not allowed_url(newurl, local=self.local):
            raise UpdateError(f"the download was sent somewhere unexpected ({urlsplit(newurl).hostname})", "bad_source")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            new.remove_header("If-none-match")
        return new


def tls_context() -> ssl.SSLContext:
    """Certificates to check GitHub's with. The packaged app's Python has no system certificate file of its
    own (python.org's build looks for one that only its installer makes), so certifi's — bundled, as httpx
    uses it — when it is there; else Python's defaults."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _opener(local: bool) -> urllib.request.OpenerDirector:
    # no cookies, no credentials; a Mac's proxy settings apply, except to the self-test's own local server
    handlers: list[Any] = [_Redirects(local), urllib.request.HTTPSHandler(context=tls_context())]
    if local:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


@dataclass(frozen=True)
class CheckResult:
    status: Literal["ok", "not_modified", "none", "offline", "rate_limited", "error"]
    release: Release | None = None
    etag: str | None = None
    retry_at: float | None = None
    """``rate_limited``: when (:func:`time.time`) GitHub allows asking again."""
    detail: str = ""


def _retry_at(headers: Any, now: float) -> float:
    """When GitHub says to ask again: ``Retry-After`` (seconds or a date), ``X-RateLimit-Reset`` (epoch), else in an hour."""
    after = headers.get("Retry-After") if headers is not None else None
    if after:
        with contextlib.suppress(ValueError, TypeError):
            return now + max(1.0, float(after))
        with contextlib.suppress(ValueError, TypeError, IndexError):
            return parsedate_to_datetime(after).timestamp()
    reset = headers.get("X-RateLimit-Reset") if headers is not None else None
    with contextlib.suppress(ValueError, TypeError):
        if reset:
            return max(now + 60.0, float(reset))
    return now + 3600.0


def fetch_release(url: str, etag: str | None = None, *, prereleases: bool = False, timeout: float = HTTP_TIMEOUT_S,
                  local: bool = False, now: Callable[[], float] = time.time) -> CheckResult:
    """Ask the releases API (blocking: run it in a thread). ``etag``: the last answer's, for a free
    "not modified". Being offline is a result like any other (``offline``), never an exception."""
    if not allowed_url(url, local=local):
        return CheckResult("error", detail="not a GitHub address")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT,
    })  # fmt: skip
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with _opener(local).open(req, timeout=timeout) as res:
            raw = res.read(MAX_FEED_BYTES + 1)
            new_etag = res.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return CheckResult("not_modified", etag=etag)
        if exc.code == 404:
            return CheckResult("none", detail="no release yet")
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers is not None else None
        if exc.code == 429 or (exc.code == 403 and (remaining == "0" or exc.headers.get("Retry-After"))):
            return CheckResult("rate_limited", retry_at=_retry_at(exc.headers, now()), detail=f"HTTP {exc.code}")
        return CheckResult("error", detail=f"HTTP {exc.code}")
    except UpdateError as exc:
        return CheckResult("error", detail=str(exc))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return CheckResult("offline", detail=str(getattr(exc, "reason", exc)))
    if len(raw) > MAX_FEED_BYTES:
        return CheckResult("error", detail="the answer is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return CheckResult("error", detail="the answer is not JSON")
    release = pick_release(data, prereleases=prereleases)
    return CheckResult("ok" if release is not None else "none", release=release, etag=new_etag)


def download(asset: Asset, folder: Path, *, on_progress: Callable[[int, int], None] | None = None,
             cancel: threading.Event | None = None, local: bool = False, timeout: float = READ_TIMEOUT_S) -> Path:
    """Download ``asset`` into ``folder`` (blocking), checking its size and SHA-256 against what the API
    said; returns the file. Refused — nothing downloaded — when the API gave no digest; a file that
    doesn't match is deleted. ``cancel`` stops it between chunks."""
    if asset.sha256 is None:
        raise UpdateError("GitHub gives no checksum for this download, so it can't be checked: not installed", "no_digest")
    if not 0 < asset.size <= MAX_DMG_BYTES:
        raise UpdateError(f"the download's size is not believable ({asset.size} bytes)", "bad_size")
    if not allowed_url(asset.url, local=local) or Path(asset.name).name != asset.name or not asset.name.endswith(".dmg"):
        raise UpdateError("the download is not where Voicestream's releases are", "bad_source")
    folder.mkdir(parents=True, exist_ok=True)
    final, part = folder / asset.name, folder / (asset.name + ".part")
    digest = hashlib.sha256()
    done = 0
    log.info("update: downloading %s (%d bytes)", asset.name, asset.size)
    req = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    try:
        with _opener(local).open(req, timeout=timeout) as res, open(part, "wb") as out:
            length = res.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) != asset.size:
                raise UpdateError(f"the download's size ({length} bytes) is not the release's ({asset.size})", "size_mismatch")
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError("cancelled", "cancelled")
                chunk = res.read(CHUNK)
                if not chunk:
                    break
                done += len(chunk)
                if done > asset.size:
                    raise UpdateError("the download is larger than the release says", "size_mismatch")
                digest.update(chunk)
                out.write(chunk)
                if on_progress is not None:
                    on_progress(done, asset.size)
    except UpdateError:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        with contextlib.suppress(OSError):
            part.unlink()
        raise UpdateError(f"the download failed: {getattr(exc, 'reason', exc)}", "download_failed") from None
    if done != asset.size:
        part.unlink(missing_ok=True)
        raise UpdateError(f"the download is incomplete ({done} of {asset.size} bytes)", "size_mismatch")
    if not hmac.compare_digest(digest.hexdigest(), asset.sha256):
        part.unlink(missing_ok=True)
        log.warning("update: %s does not match its SHA-256: deleted", asset.name)
        raise UpdateError("the download doesn't match the release's checksum: it was deleted, nothing was installed", "digest_mismatch")
    os.replace(part, final)
    log.info("update: %s downloaded, size and SHA-256 match", asset.name)
    return final


def verify_file(path: Path, asset: Asset) -> bool:
    """A DMG downloaded earlier is still the release's (size and SHA-256), before it is used."""
    if asset.sha256 is None:
        return False
    try:
        if path.stat().st_size != asset.size:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                h.update(chunk)
    except OSError:
        return False
    return hmac.compare_digest(h.hexdigest(), asset.sha256)


# ------------------------------------------------------------------------- the running app


def running_app() -> Path | None:
    """The running Voicestream.app, from the executable (``<app>/Contents/MacOS/Voicestream``); ``None``
    outside the packaged app."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    app = Path(sys.executable).resolve().parents[2]
    return app if app.suffix == ".app" else None


def install_problem(app: Path | None) -> tuple[str, str] | None:
    """Why this copy of Voicestream can't replace itself — ``(code, words)`` — or ``None``."""
    if app is None:
        return "not_packaged", "updates install by themselves only in the Voicestream app for Mac"
    if "AppTranslocation" in app.parts:
        return "translocated", "Voicestream runs from a temporary copy macOS made of the download (App Translocation)"
    if app.parts[:2] == ("/", "Volumes") or _read_only(app):
        return "on_dmg", "Voicestream runs from its disk image"
    if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
        return "not_writable", f"Voicestream can't replace itself in {app.parent} (no permission to write there)"
    return None


def _read_only(path: Path) -> bool:
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        return False


def app_management_blocked(app: Path) -> bool:
    """Whether macOS refuses a write inside the app's own bundle (App Management, macOS 13+, answers
    EPERM): a file created and removed at once in ``Contents``, which changes nothing that is signed."""
    probe = app / "Contents" / f".voicestream-update-probe-{os.getpid()}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except PermissionError:
        return True
    except OSError as exc:
        log.info("update: could not test writing in %s: %s", app, exc)
        return False
    os.close(fd)
    with contextlib.suppress(OSError):
        probe.unlink()
    return False


# ------------------------------------------------------------------------ the new bundle


Run = Callable[..., subprocess.CompletedProcess]


def _run(args: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL)


def mount(dmg: Path, root: Path, run: Run = _run) -> Path:
    """Attach ``dmg`` read-only, hidden from Finder and the desktop, under ``root``; returns the volume."""
    root.mkdir(parents=True, exist_ok=True)
    proc = run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountrandom", str(root),
                "-plist", str(dmg)])
    if proc.returncode != 0:
        raise UpdateError(f"the update's disk image could not be opened: {proc.stderr.strip()[:300]}", "mount_failed")
    try:
        info = plistlib.loads(proc.stdout.encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise UpdateError(f"hdiutil said something unexpected: {exc}", "mount_failed") from None
    points = [e.get("mount-point") for e in info.get("system-entities") or [] if e.get("mount-point")]
    if not points:
        raise UpdateError("the update's disk image has no volume", "mount_failed")
    return Path(points[0])


def unmount(volume: Path, run: Run = _run) -> None:
    for extra in ([], ["-force"]):
        proc = run(["/usr/bin/hdiutil", "detach", *extra, str(volume)], 60.0)
        if proc.returncode == 0:
            return
        time.sleep(1.0)
    log.warning("update: could not detach %s", volume)


def macho_archs(path: Path) -> set[str]:
    """The architectures a Mach-O file (thin or universal) has code for: ``{"arm64"}``, ``{"x86_64", "arm64"}``…"""
    names = {ARM64: "arm64", X86_64: "x86_64"}
    with open(path, "rb") as fh:
        head = fh.read(8)
        if len(head) < 8:
            return set()
        magic_be = struct.unpack(">I", head[:4])[0]
        if magic_be in (0xCAFEBABE, 0xCAFEBABF):  # universal: big-endian header, then one entry per architecture
            count = struct.unpack(">I", head[4:])[0]
            size = 20 if magic_be == 0xCAFEBABE else 32
            out = set()
            for _ in range(min(count, 16)):
                entry = fh.read(size)
                if len(entry) < 8:
                    break
                out.add(names.get(struct.unpack(">i", entry[:4])[0] & 0xFFFFFFFF, "other"))
            return out
        magic_le = struct.unpack("<I", head[:4])[0]
        if magic_le in (0xFEEDFACF, 0xFEEDFACE):
            return {names.get(struct.unpack("<i", head[4:])[0] & 0xFFFFFFFF, "other")}
    return set()


def bundle_problems(app: Path, version: Version | str, run: Run = _run) -> list[str]:
    """What is wrong with the Voicestream.app an update brings (empty: nothing): our bundle id, the release's
    version, an arm64 executable, a signature ``codesign --verify --deep --strict`` accepts."""
    out: list[str] = []
    if app.is_symlink() or not app.is_dir():
        return [f"{app.name} is not an app"]
    try:
        with open(app / "Contents" / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        return [f"its Info.plist can't be read ({exc})"]
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        out.append(f"it is another app ({info.get('CFBundleIdentifier')!r})")
    if str(info.get("CFBundleShortVersionString")) != str(version):
        out.append(f"its version is {info.get('CFBundleShortVersionString')!r}, not {version}")
    exe = app / "Contents" / "MacOS" / str(info.get("CFBundleExecutable") or APP_NAME)
    try:
        if "arm64" not in macho_archs(exe):
            out.append("it has no code for Apple Silicon (arm64)")
    except OSError as exc:
        out.append(f"its program can't be read ({exc})")
    proc = run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    if proc.returncode != 0:
        out.append(f"its signature is broken: {(proc.stderr or proc.stdout).strip()[:300]}")
    return out


def staged_path(app: Path, version: Version | str) -> Path:
    """Where the new version waits next to the running app (the same volume: moving it in is a rename)."""
    return app.parent / f".{APP_NAME}-{version}.update.app"


def stage(src: Path, dest: Path, run: Run = _run) -> Path:
    """Copy the new app with ``ditto`` (symlinks, modes and the signature intact; ``--noqtn``: no quarantine)."""
    if dest.exists() or dest.is_symlink():
        shutil.rmtree(dest, ignore_errors=True)
    proc = run(["/usr/bin/ditto", "--noqtn", str(src), str(dest)], 900.0)
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise UpdateError(f"the new version could not be copied next to the app: {proc.stderr.strip()[:300]}", "stage_failed")
    return dest


def quarantined(path: Path, run: Run = _run) -> bool:
    """Whether ``path`` carries ``com.apple.quarantine`` (macOS's ``xattr``)."""
    return run(["/usr/bin/xattr", "-p", "com.apple.quarantine", str(path)], 30.0).returncode == 0


# --------------------------------------------------------------------------- the helper


HELPER = r"""#!/bin/sh
# Voicestream's update helper, written by voicestream/app/updater.py into the app's support folder.
# It runs on its own, after Voicestream has quit: it puts the new version in the old one's place and
# opens it. Every path comes in as an argument, never pasted into this text, so spaces or quotes in
# them are safe.
#
#   update-helper.sh <pid> <app> <new app> <dmg> <result file> <log file> <relaunch: 1 or 0>
#
# The old version is moved aside first and removed only once the new one is in place; if anything
# fails, it goes back where it was and opens again, with the verified update open in Finder to drag
# in by hand: you are never left without a working Voicestream.
set -u
pid=$1 app=$2 new=$3 dmg=$4 result=$5 log=$6 relaunch=$7
mv=/bin/mv
rm=/bin/rm
open=/usr/bin/open
xattr=/usr/bin/xattr
codesign=/usr/bin/codesign
old="${app%/*}/.Voicestream-previous-$$.app"  # the same folder: moving it back is a rename too

say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$log"; }
finish() { printf '%s\n' "$1" >"$result"; say "$1"; }
restore() {
  if [ ! -e "$app" ] && [ -e "$old" ]; then
    "$mv" "$old" "$app" || say "could not put the previous version back: it is at $old"
  fi
}
give_up() {  # give_up <code> <what happened>
  finish "failed: $1: $2"
  restore
  if [ -e "$new" ]; then "$rm" -rf "$new"; fi
  if [ "$relaunch" = 1 ]; then
    if [ -f "$dmg" ]; then "$open" "$dmg" || say "could not open $dmg"; fi
    "$open" "$app" || say "could not open $app"
  fi
  exit 1
}

waited=0
while kill -0 "$pid" 2>/dev/null; do
  if [ "$waited" -ge __WAIT__ ]; then
    finish "failed: not_quit: Voicestream did not quit"
    "$rm" -rf "$new"
    exit 1
  fi
  sleep 0.5
  waited=$((waited + 1))
done
say "Voicestream quit: replacing $app"
if [ ! -d "$new" ]; then give_up missing "the new version is missing"; fi
if ! "$mv" "$app" "$old"; then give_up app_management "macOS did not let the old version be moved"; fi
if ! "$mv" "$new" "$app"; then give_up move_failed "the new version could not be moved in place"; fi
"$xattr" -dr com.apple.quarantine "$app" 2>/dev/null || true
if ! "$codesign" --verify --deep --strict "$app" >>"$log" 2>&1; then
  "$rm" -rf "$app"
  give_up bundle_invalid "the new version's signature broke on the way"
fi
"$rm" -rf "$old" || say "could not remove the previous version at $old"
finish "installed"
if [ "$relaunch" = 1 ]; then
  "$open" "$app" || say "could not open $app"
fi
exit 0
""".replace("__WAIT__", str(int(HELPER_WAIT_S * 2)))


def write_helper(folder: Path) -> Path:
    """The helper script in ``folder`` (the support folder), readable and runnable by you only."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "update-helper.sh"
    tmp = folder / f".update-helper.{os.getpid()}.tmp"
    tmp.write_text(HELPER, encoding="utf-8")
    os.chmod(tmp, stat.S_IRWXU)
    os.replace(tmp, path)
    return path


def start_helper(script: Path, *, pid: int, app: Path, new: Path, dmg: Path, result: Path, log_file: Path,
                 relaunch: bool = True) -> subprocess.Popen:
    """Start the helper detached: its own session (``setsid``), so it isn't in the app's process group and
    outlives it; no inherited descriptors, nothing to read or write but its log."""
    args = ["/bin/sh", str(script), str(pid), str(app), str(new), str(dmg), str(result), str(log_file),
            "1" if relaunch else "0"]
    log.info("update: starting the helper (it waits for process %d to quit)", pid)
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True, start_new_session=True, cwd="/")


def open_in_finder(path: Path) -> None:
    """The fallback: open the verified DMG, so the new version is one drag away."""
    if sys.platform == "darwin":
        with contextlib.suppress(OSError):
            subprocess.Popen(["/usr/bin/open", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)


# ------------------------------------------------------------------------- state machine


class InvalidTransition(UpdateError):
    pass


@dataclass
class UpdateMachine:
    """Where the update stands, and the only moves between those places:

    ``idle`` (nothing newer) → ``available`` (a newer release) → ``downloading`` → ``ready`` (downloaded
    and verified) → ``installing`` (the helper is started, the app quits); any of the middle ones →
    ``failed`` (with :attr:`error`; the release is kept, so it can be tried again). A check that finds
    the same release changes nothing; one that finds a newer one starts over at ``available``.

    :attr:`deferred`: Install was asked for during a meeting. The download goes on, nothing is installed;
    when the meeting ends (:meth:`meeting_over`) the window offers it again (:attr:`offer`)."""

    current: Version = field(default_factory=current_version)
    phase: Phase = "idle"
    release: Release | None = None
    error: str | None = None
    error_code: str | None = None
    """What :attr:`error` is about, for the window's words (``digest_mismatch``, ``app_management``…)."""
    progress: tuple[int, int] | None = None
    deferred: bool = False
    offer: bool = False
    """The meeting that held the install back has ended: say it can be installed now."""

    def found(self, release: Release | None) -> bool:
        """A check's answer; returns whether the state changed."""
        if release is None or release.version <= self.current:
            if self.phase in ("available", "failed"):
                # nothing newer than this version any more (the release was pulled): nothing to offer
                self.phase, self.release, self.error, self.error_code = "idle", None, None, None
                self.deferred = self.offer = False
                return True
            return False
        if self.release is not None and release.version == self.release.version:
            if self.phase != "installing":
                self.release = release  # notes edited, a digest added since: the latest word on it
            return False
        if self.phase in ("downloading", "installing"):
            return False  # the one under way finishes first; the next check offers the newer one
        self.phase, self.release, self.error, self.error_code, self.progress = "available", release, None, None, None
        self.deferred = self.offer = False
        return True

    def start_download(self) -> None:
        if self.release is None or self.phase not in ("available", "failed", "ready"):
            raise InvalidTransition(f"nothing to download ({self.phase})")
        self.phase, self.error, self.error_code = "downloading", None, None
        self.progress = (0, self.release.asset.size if self.release.asset else 0)

    def advance(self, done: int, total: int) -> None:
        if self.phase == "downloading":
            self.progress = (done, total)

    def downloaded(self) -> None:
        if self.phase != "downloading":
            raise InvalidTransition(f"no download under way ({self.phase})")
        self.phase = "ready"
        self.progress = None

    def defer(self) -> None:
        if self.release is None or self.phase in ("idle", "installing"):
            raise InvalidTransition(f"nothing to install ({self.phase})")
        self.deferred, self.offer = True, False

    def meeting_over(self) -> bool:
        """The meeting that held the install back is over; returns whether that changes anything."""
        if not self.deferred:
            return False
        self.deferred, self.offer = False, True
        return True

    def installing(self) -> None:
        if self.phase != "ready":
            raise InvalidTransition(f"not downloaded ({self.phase})")
        self.phase, self.deferred, self.offer = "installing", False, False

    def fail(self, error: str, code: str = "unexpected") -> None:
        if self.release is None:
            raise InvalidTransition("no update to fail")
        self.phase, self.error, self.error_code, self.progress, self.deferred = "failed", error, code, None, False

    def view(self) -> dict[str, Any]:
        done, total = self.progress or (0, 0)
        return {
            "current": str(self.current), "phase": self.phase, "error": self.error, "error_code": self.error_code,
            "deferred": self.deferred,
            "offer": self.offer, "release": self.release.as_dict() if self.release else None,
            "progress": {"done": done, "total": total} if self.progress else None,
        }  # fmt: skip


# ----------------------------------------------------------------------- the host's part


class Updater:
    """The update as the app host runs it, on its loop (blocking steps in worker threads): checks on a
    schedule and on demand, the download, the checks of the new bundle, the helper. ``on_change`` is
    called (on the loop) when what the window shows changes; ``busy()`` says whether a meeting is being
    recorded, when nothing may be installed; ``quit_app`` (set by the native shell) quits the app
    gracefully once the helper waits for it.

    ``folder`` holds the downloads, the helper, its log and a small state file (when it was last checked,
    the last answer's ETag, the version put off with Later); ``None``: nothing kept on disk (tests)."""

    def __init__(self, folder: Path | None, *, on_change: Callable[[], None], busy: Callable[[], bool],
                 feed: str | None = None, local: bool = False, now: Callable[[], float] = time.time) -> None:
        self.folder = folder
        self.on_change = on_change
        self.busy = busy
        self.feed = feed
        self.local = local
        self.now = now
        self.machine = UpdateMachine()
        self.checking = False
        self.last_checked: float | None = None
        self.last_result: str | None = None
        self.etag: str | None = None
        self.etag_url: str | None = None
        """What :attr:`etag` was the answer to (the latest release, or the list with pre-releases)."""
        self.cached: Release | None = None
        self.later: str | None = None
        """The version put off with Later: no banner for it (the menu still offers it)."""
        self.retry_at: float | None = None
        self.checked_this_launch = False
        self.outcome: dict[str, Any] | None = None
        """What the helper did after the last quit for an update (``installed``, ``failed: …``), shown once."""
        self.can_install = False
        """The native shell says this is the packaged app, which can replace itself."""
        self.quit_app: Callable[[], None] | None = None
        self.dmg: Path | None = None
        self._cancel = threading.Event()
        self._task: asyncio.Task[Any] | None = None
        self._last_progress = 0.0
        self._load()

    # ------------------------------------------------------------------ state file

    @property
    def _state_file(self) -> Path | None:
        return self.folder / "state.json" if self.folder is not None else None

    def _load(self) -> None:
        path = self._state_file
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            self.last_checked = data.get("last_checked") if isinstance(data.get("last_checked"), (int, float)) else None
            self.last_result = data.get("last_result") if isinstance(data.get("last_result"), str) else None
            self.etag = data.get("etag") if isinstance(data.get("etag"), str) else None
            self.etag_url = data.get("etag_url") if isinstance(data.get("etag_url"), str) else None
            self.later = data.get("later") if isinstance(data.get("later"), str) else None
            cached = data.get("release")
            self.cached = Release.from_dict(cached) if isinstance(cached, dict) else None
            if self.cached is None:
                self.etag = None  # a "not modified" would have nothing to stand for
        assert self.folder is not None
        result = self.folder / "result"
        with contextlib.suppress(OSError):
            text = result.read_text(encoding="utf-8").strip()[:300]
            result.unlink()
            if text:
                # "installed", or "failed: <code>: <what happened>" (HELPER's give_up)
                code = text.removeprefix("failed: ").split(":", 1)[0].strip() if text != "installed" else None
                self.outcome = {"kind": "installed" if code is None else "failed", "detail": code}
                log.info("update: the helper says %s", text)
        self._tidy()

    def _save(self) -> None:
        path = self._state_file
        if path is None:
            return
        from .files import write_private

        data = {"last_checked": self.last_checked, "last_result": self.last_result, "etag": self.etag,
                "etag_url": self.etag_url,
                "later": self.later, "release": self.cached.as_dict() if self.cached else None}
        try:
            write_private(path, json.dumps(data, indent=1) + "\n")
        except OSError as exc:
            log.warning("update: could not save its state: %s", exc)

    def _tidy(self) -> None:
        """Downloads of this version or older, and half downloads, are of no use any more."""
        if self.folder is None or not self.folder.is_dir():
            return
        for f in self.folder.glob(f"{APP_NAME}-*-arm64.dmg*"):
            v = parse_version(f.name.removeprefix(f"{APP_NAME}-").split("-arm64")[0])
            if f.name.endswith(".part") or v is None or v <= self.machine.current:
                with contextlib.suppress(OSError):
                    f.unlink()

    # ------------------------------------------------------------------------ view

    def view(self, *, prefs_on: bool = True) -> dict[str, Any]:
        """``state()["update"]``: what the window, Settings and the menu bar show."""
        out = self.machine.view()
        out.update(
            checking=self.checking, can_install=self.can_install, later=self.later, outcome=self.outcome,
            last_checked=datetime.fromtimestamp(self.last_checked, UTC).isoformat() if self.last_checked else None,
            last_result=self.last_result, releases_page=RELEASES_PAGE, automatic=prefs_on,
        )  # fmt: skip
        return out

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            log.exception("update: publishing its state failed")

    # ---------------------------------------------------------------------- check

    def due(self, every: float = CHECK_EVERY_S) -> bool:
        """Time for an automatic check: once at each launch, then every ``every`` seconds, never before
        GitHub's rate limit allows it."""
        now = self.now()
        if self.retry_at is not None and now < self.retry_at:
            return False
        if self.checking or self.machine.phase in ("downloading", "installing"):
            return False
        return not self.checked_this_launch or self.last_checked is None or now - self.last_checked >= every

    async def check(self, *, prereleases: bool = False, manual: bool = False) -> str:
        """Ask GitHub for the latest release; returns how it went: ``available``, ``up_to_date``,
        ``offline``, ``rate_limited``, ``error`` (or ``busy`` while one is under way)."""
        if self.checking:
            return "busy"
        self.checking = True
        self.checked_this_launch = True
        self._changed()
        url = self.feed or (LIST_URL if prereleases else LATEST_URL)
        etag = self.etag if self.cached is not None and self.etag_url == url else None
        try:
            res = await asyncio.to_thread(fetch_release, url, etag, prereleases=prereleases, local=self.local,
                                          now=self.now)
        finally:
            self.checking = False
        outcome = self._take(res, prereleases, url)
        if outcome not in ("offline",) or manual:
            log.info("update: checked for a new version (%s%s)", outcome, f": {res.detail}" if res.detail else "")
        self._changed()
        return outcome

    def _take(self, res: CheckResult, prereleases: bool, url: str) -> str:
        now = self.now()
        if res.status == "rate_limited":
            self.retry_at = res.retry_at
            self.last_result = "rate_limited"
            return "rate_limited"
        if res.status in ("offline", "error"):
            self.retry_at = now + 15 * 60.0  # quietly try again a bit later
            self.last_result = res.status
            return res.status
        self.retry_at = None
        if res.status == "not_modified":
            release = self.cached
        else:
            release = res.release
            self.cached, self.etag, self.etag_url = release, (res.etag if release is not None else None), url
        if release is not None and release.prerelease and not prereleases:
            release = None
        self.last_checked = now
        self.machine.found(release)
        newer = release is not None and release.version > self.machine.current
        self.last_result = "available" if newer else "up_to_date"
        self._save()
        return self.last_result

    # ----------------------------------------------------------------- user choices

    def put_off(self) -> bool:
        """Later: no banner for this version (the menu and Settings still offer it); also clears an outcome."""
        r = self.machine.release
        changed = self.outcome is not None
        self.outcome = None
        if r is not None and self.later != str(r.version):
            self.later = str(r.version)
            self.machine.offer = False
            self._save()
            changed = True
        if changed:
            self._changed()
        return changed

    def cancel(self) -> bool:
        if self.machine.phase != "downloading":
            return False
        self._cancel.set()
        return True

    def meeting_over(self) -> None:
        """The host: nothing is recorded any more (called at each state; cheap)."""
        if self.machine.deferred and not self.busy() and self.machine.meeting_over():
            log.info("update: the meeting is over, the update can be installed now")
            self.later = None
            self._changed()

    # ---------------------------------------------------------------------- install

    def install(self, app: Path | None) -> str:
        """Install and restart (on the loop): returns at once — ``deferred`` (a meeting is being recorded: once
        it ends), ``started``, ``busy`` (one is under way) or ``none``. The work goes on in :meth:`_install`."""
        m = self.machine
        if m.release is None or m.phase == "idle":
            return "none"
        if m.phase == "installing" or (self._task is not None and not self._task.done()):
            return "busy"
        self.later = None
        if self.busy():
            m.defer()
            log.info("update: install asked for during a meeting: it waits for the meeting to end")
            if m.phase in ("available", "failed"):
                self._task = asyncio.ensure_future(self._download_only())
            self._changed()
            return "deferred"
        m.offer = False
        self._task = asyncio.ensure_future(self._install(app))
        return "started"

    async def _fetch(self) -> Path:
        """The verified DMG of the release on offer: the one downloaded before if it still matches."""
        m = self.machine
        assert m.release is not None
        asset = m.release.asset
        if asset is None:
            raise UpdateError(f"the release has no {asset_name(m.release.version)} to download", "no_asset")
        if self.folder is None:
            raise UpdateError("there is nowhere to download the update to", "unexpected")
        dmg = self.folder / asset.name
        if m.phase == "ready" and self.dmg == dmg and await asyncio.to_thread(verify_file, dmg, asset):
            return dmg
        if m.phase == "ready":
            m.phase = "available"
        m.start_download()
        self._cancel.clear()
        self._changed()
        loop = asyncio.get_running_loop()

        def progress(done: int, total: int) -> None:
            loop.call_soon_threadsafe(self._progress, done, total)

        self.dmg = await asyncio.to_thread(download, asset, self.folder, on_progress=progress, cancel=self._cancel,
                                           local=self.local)
        m.downloaded()
        self._changed()
        return self.dmg

    def _progress(self, done: int, total: int) -> None:
        self.machine.advance(done, total)
        now = time.monotonic()
        if now - self._last_progress >= 0.25 or done >= total:
            self._last_progress = now
            self._changed()

    async def _download_only(self) -> None:
        try:
            await self._fetch()
        except UpdateError as exc:
            self._failed(str(exc), exc.code)
        except OSError as exc:
            self._failed(f"the download could not be saved: {exc}", "download_failed")

    def _failed(self, message: str, code: str = "unexpected") -> None:
        log.warning("update: %s", message)
        if self.machine.release is not None:
            deferred = self.machine.deferred
            self.machine.fail(message, code)
            self.machine.deferred = deferred and code != "cancelled"
        self._changed()

    async def _install(self, app: Path | None) -> None:
        m = self.machine
        assert m.release is not None
        version = m.release.version
        try:
            problem = install_problem(app) if self.can_install else install_problem(None)
            if problem:
                raise UpdateError(problem[1], problem[0])
            assert app is not None
            dmg = await self._fetch()
            if self.busy():  # a meeting started during the download
                m.defer()
                self._changed()
                return
            try:
                staged = await asyncio.to_thread(self._prepare, dmg, app, version)
            except AppManagementBlocked:
                # the swap would fail too: the verified update, open in Finder, is one drag away instead
                log.warning("update: macOS refuses changes to %s (App Management): opening the update in Finder", app)
                open_in_finder(dmg)
                self._failed("macOS refuses changes to the app's bundle (App Management)", "app_management")
                return
            if self.busy():
                await asyncio.to_thread(shutil.rmtree, staged, True)
                m.defer()
                self._changed()
                return
            if self.quit_app is None:
                raise UpdateError("Voicestream can't restart itself here", "cannot_restart")
            assert self.folder is not None
            script = write_helper(self.folder)
            start_helper(script, pid=os.getpid(), app=app, new=staged, dmg=dmg, result=self.folder / "result",
                         log_file=self.folder / "update.log")
            m.installing()
            self._changed()
            log.info("update: %s is ready, quitting so the helper can put it in place", version)
            self.quit_app()
        except UpdateError as exc:
            self._failed(str(exc), exc.code)
        except Exception as exc:  # never a broken app: say it and leave everything as it was
            log.exception("update: failed")
            self._failed(f"{type(exc).__name__}: {exc}")

    def _prepare(self, dmg: Path, app: Path, version: Version, run: Run = _run) -> Path:
        """(Worker thread.) Mount, check the new app, stage it next to ``app``, unmount. Raises
        :class:`AppManagementBlocked` first when macOS won't let the app change its own bundle."""
        assert self.folder is not None
        if app_management_blocked(app):
            raise AppManagementBlocked("macOS refuses changes to the app's bundle", "app_management")
        volume = mount(dmg, self.folder / "mnt", run)
        log.info("update: mounted the update at %s", volume)
        try:
            new = volume / f"{APP_NAME}.app"
            problems = bundle_problems(new, version, run)
            if problems:
                raise UpdateError("the update's app failed its checks: " + "; ".join(problems), "bundle_invalid")
            log.info("update: the new app is Voicestream %s, signed, arm64", version)
            staged = stage(new, staged_path(app, version), run)
            problems = bundle_problems(staged, version, run)
            if problems:
                shutil.rmtree(staged, ignore_errors=True)
                raise UpdateError("the copy of the new app failed its checks: " + "; ".join(problems), "bundle_invalid")
            log.info("update: staged at %s", staged)
            return staged
        finally:
            unmount(volume, run)


# --------------------------------------------------------------------------- self-test


SELFTEST_DMG_ENV = "VOICESTREAM_SELFTEST_UPDATE_DMG"
"""The test DMG ``build.sh selftest`` makes; read by the packaged app's self-test only (never by an update)."""
SELFTEST_CHECKS = ("update_check", "update_digest_refused", "update_download_verified", "update_bundle_valid",
                   "update_swap")


class _Feed(http.server.ThreadingHTTPServer):
    daemon_threads = True


def _feed_server(routes: dict[str, tuple[bytes | Path, str, str | None]]) -> _Feed:
    """A local stand-in for GitHub (the self-test only): ``routes`` path → (body or file, content type, ETag)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            found = routes.get(self.path)
            if found is None:
                self.send_error(404)
                return
            body, ctype, etag = found
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(body.stat().st_size if isinstance(body, Path) else len(body)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            if isinstance(body, Path):
                with open(body, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, CHUNK)
            else:
                self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = _Feed(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, name="voicestream-update-feed", daemon=True).start()
    return server


def selftest(dmg: Path, app: Path | None, work: Path, run: Run = _run) -> dict[str, Any]:
    """The packaged app's proof that updating works, without the network (``packaging/mac/build.sh selftest``
    builds ``dmg``: this app with a newer version, re-signed). Against a local stand-in for GitHub's API:
    check (and check again: the ETag's "not modified"; the certificates GitHub's TLS is checked with are
    there), refuse a download whose digest is wrong, download and verify the right one (no quarantine on it),
    mount it, check its app, stage it next to a temporary copy of this app (never the real one), run the
    helper against that copy, and read the swapped bundle's version.
    Returns the report; its ``checks`` say what passed."""
    out: dict[str, Any] = {"checks": {}}
    checks = out["checks"]
    m = re.fullmatch(rf"{APP_NAME}-(.+)-arm64\.dmg", dmg.name)
    version = parse_version(m.group(1)) if m else None
    if version is None or not dmg.is_file():
        out["error"] = f"no test DMG named {APP_NAME}-<version>-arm64.dmg at {dmg}"
        return out
    sha = hashlib.sha256()
    with open(dmg, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            sha.update(chunk)
    size = dmg.stat().st_size
    out["version"] = str(version)

    def release(digest: str, path: str, port: int) -> bytes:
        return json.dumps({
            "tag_name": f"v{version}", "name": f"{APP_NAME} v{version}", "draft": False, "prerelease": False,
            "html_url": RELEASES_PAGE, "body": "### New in this version\n\n- **Updates** install from inside the app.\n",
            "assets": [{"name": dmg.name, "size": size, "digest": f"sha256:{digest}",
                        "browser_download_url": f"http://127.0.0.1:{port}{path}"}],
        }).encode()  # fmt: skip

    routes: dict[str, tuple[bytes | Path, str, str | None]] = {}
    server = _feed_server(routes)
    port = server.server_address[1]
    try:
        routes["/latest"] = (release(sha.hexdigest(), "/dmg", port), "application/json", '"e1"')
        routes["/wrong"] = (release("0" * 64, "/dmg", port), "application/json", None)
        routes["/dmg"] = (dmg, "application/octet-stream", None)
        base = f"http://127.0.0.1:{port}"

        first = fetch_release(f"{base}/latest", local=True)
        again = fetch_release(f"{base}/latest", first.etag, local=True)
        r = first.release
        # the real check reaches GitHub over TLS: the packaged app must have certificates to check it with
        cas = tls_context().cert_store_stats().get("x509_ca", 0)
        out["check"] = {"status": first.status, "found": str(r.version) if r else None, "etag": first.etag is not None,
                        "again": again.status, "notes": r.notes if r else None, "tls_certificates": cas}
        checks["update_check"] = (first.status == "ok" and r is not None and r.version == version
                                  and r.version > current_version() and again.status == "not_modified"
                                  and bool(r.notes) and r.asset is not None and r.asset.sha256 == sha.hexdigest()
                                  and cas > 0)

        downloads = work / "downloads"
        wrong = fetch_release(f"{base}/wrong", local=True).release
        try:
            assert wrong is not None and wrong.asset is not None
            download(wrong.asset, downloads, local=True)
            out["wrong_digest"] = "accepted"
        except UpdateError as exc:
            out["wrong_digest"] = f"refused: {exc}"
        checks["update_digest_refused"] = (out["wrong_digest"].startswith("refused")
                                           and not (downloads / dmg.name).exists()
                                           and not (downloads / (dmg.name + ".part")).exists())

        assert r is not None and r.asset is not None
        got = download(r.asset, downloads, local=True)
        out["download"] = {"bytes": got.stat().st_size, "quarantined": quarantined(got, run),
                           "verified": verify_file(got, r.asset)}
        checks["update_download_verified"] = out["download"]["verified"] and not out["download"]["quarantined"]

        if app is None:
            out["error"] = "not the packaged app: nothing to copy"
            return out
        old_parent = work / "Applications"
        old_parent.mkdir(parents=True, exist_ok=True)
        old = old_parent / f"{APP_NAME}.app"
        proc = run(["/usr/bin/ditto", str(app), str(old)], 900.0)
        if proc.returncode != 0:
            out["error"] = f"could not copy the app for the test: {proc.stderr.strip()[:300]}"
            return out
        out["install_problem"] = install_problem(old)
        out["app_management_blocked"] = app_management_blocked(old)
        volume = mount(got, work / "mnt", run)
        try:
            out["volume_read_only"] = _read_only(volume)
            new = volume / f"{APP_NAME}.app"
            out["bundle_problems"] = problems = bundle_problems(new, version, run)
            checks["update_bundle_valid"] = not problems and out["volume_read_only"]
            staged = stage(new, staged_path(old, version), run) if not problems else None
        finally:
            unmount(volume, run)
        if staged is None:
            return out
        out["staged_problems"] = bundle_problems(staged, version, run)
        stand_in = subprocess.Popen(["/bin/sleep", "1"])
        helper = start_helper(write_helper(work), pid=stand_in.pid, app=old, new=staged, dmg=got,
                              result=work / "result", log_file=work / "update.log", relaunch=False)
        stand_in.wait(10)
        helper.wait(120)
        with open(old / "Contents" / "Info.plist", "rb") as fh:
            swapped = str(plistlib.load(fh).get("CFBundleShortVersionString"))
        leftovers = [p.name for p in old_parent.iterdir() if p.name != old.name]
        out["swap"] = {"helper_exit": helper.returncode, "result": (work / "result").read_text().strip(),
                       "version": swapped, "leftovers": leftovers, "quarantined": quarantined(old, run),
                       "signature_ok": run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(old)]).returncode == 0}
        checks["update_swap"] = (helper.returncode == 0 and out["swap"]["result"] == "installed" and swapped == str(version)
                                 and not leftovers and not out["swap"]["quarantined"] and out["swap"]["signature_ok"]
                                 and not out["staged_problems"])
        return out
    except Exception as exc:  # noqa: BLE001 - in the report
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        server.shutdown()
        server.server_close()


def selftest_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="voicestream-update-test-"))
