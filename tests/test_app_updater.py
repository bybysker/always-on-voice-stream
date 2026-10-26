"""In-app updates (voicestream/app/updater.py): versions, releases, digests, the state machine, the helper
script, and the host's routes — all without the network (a local stand-in for GitHub) and without a Mac
(macOS's tools are stand-ins; the packaged app's self-test runs the real ones on the macOS CI)."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_app_host import _factory, _write_config

from voicestream import __version__
from voicestream.app import updater as upd
from voicestream.app.host import AppHost
from voicestream.dashboard.server import TOKEN_HEADER, create_app

ROOT = Path(__file__).resolve().parents[1]
NOTES = (ROOT / "packaging" / "mac" / "release-notes.md").read_text(encoding="utf-8")
TOKEN = "update-token-1"
BASE = "http://127.0.0.1:8765"


def V(text: str) -> upd.Version:
    v = upd.parse_version(text)
    assert v is not None, text
    return v


# ------------------------------------------------------------------------------ versions


def test_versions_are_ordered_as_pep_440_orders_them() -> None:
    ordered = ["0.3.0", "0.4.0.dev1", "0.4.0a1", "0.4.0-beta.1", "0.4.0b2", "0.4.0rc1", "0.4.0", "0.4.0.post1", "0.4.1",
               "0.10.0", "1.0.0"]
    parsed = [V(v) for v in ordered]
    assert sorted(parsed[5:] + parsed[:5], key=lambda v: v.key) == parsed
    assert all(a < b for a, b in itertools.pairwise(parsed))
    assert V("0.4") == V("0.4.0") == V("v0.4.0") and str(V("v0.4.0")) == "0.4.0"
    assert V("0.10.0") > V("0.9.9")  # numbers, not text
    assert V("0.4.0-rc.1") == V("0.4.0rc1") and V("1.0.0-alpha") < V("1.0.0-alpha.1") < V("1.0.0-beta")


def test_prereleases_and_what_is_not_a_version() -> None:
    assert V("0.4.0rc1").is_prerelease and V("0.4.0.dev3").is_prerelease and V("0.4.0-beta.2").is_prerelease
    assert not V("0.4.0").is_prerelease and not V("0.4.0.post2").is_prerelease
    for bad in ("", "latest", "v", "0.4.0 beta", "0.4.0;rm -rf /", "nightly-2026-10-26", None):
        assert upd.parse_version(bad) is None
    assert upd.current_version() == V(__version__)


# ------------------------------------------------------------------------------ releases


def _release(tag: str = "v0.4.0", *, digest: str | None = "sha256:" + "ab" * 32, size: int = 1234, draft: bool = False,
             prerelease: bool = False, body: str = NOTES, name: str | None = None) -> dict[str, Any]:
    version = tag.removeprefix("v")
    asset: dict[str, Any] = {"name": name or f"Voicestream-{version}-arm64.dmg", "size": size,
                             "browser_download_url": f"https://github.com/bybysker/always-on-voice-stream/releases/download/{tag}/x.dmg"}
    if digest is not None:
        asset["digest"] = digest
    return {"tag_name": tag, "name": f"Voicestream {tag}", "draft": draft, "prerelease": prerelease, "body": body,
            "html_url": f"https://github.com/bybysker/always-on-voice-stream/releases/tag/{tag}",
            "published_at": "2026-10-20T10:00:00Z",
            "assets": [{"name": "Source.zip", "size": 1, "browser_download_url": "https://example.com/s"}, asset]}


def test_a_release_is_read_from_the_api() -> None:
    r = upd.parse_release(_release())
    assert r is not None and r.version == V("0.4.0") and r.tag == "v0.4.0" and not r.prerelease
    assert r.asset == upd.Asset("Voicestream-0.4.0-arm64.dmg", r.asset.url, 1234, "ab" * 32)
    assert r.notes.startswith("• Meetings start when you say so.") and "### Install" not in r.notes
    assert upd.Release.from_dict(r.as_dict()) == r  # cached behind its ETag
    # no digest: the release is offered, but its file can't be checked (download refuses it)
    assert upd.parse_release(_release(digest=None)).asset.sha256 is None
    assert upd.parse_release(_release(digest="sha1:" + "ab" * 20)).asset.sha256 is None
    # the DMG must be the one named for this version (mac-app.yml checks the same when it publishes)
    assert upd.parse_release(_release(name="Voicestream-0.3.0-arm64.dmg")).asset is None
    assert upd.parse_release(_release(draft=True)) is None
    assert upd.parse_release(_release(tag="nightly")) is None
    assert upd.parse_release(["not", "an", "object"]) is None


def test_digests() -> None:
    assert upd.parse_digest("sha256:" + "AB" * 32) == "ab" * 32
    for bad in (None, "", "sha256:", "sha256:" + "ab" * 31, "sha256:" + "zz" * 32, "md5:" + "ab" * 16, "ab" * 32):
        assert upd.parse_digest(bad) is None


def test_drafts_never_prereleases_only_when_asked() -> None:
    items = [_release("v0.4.0"), _release("v0.5.0rc1", prerelease=True), _release("v0.6.0", draft=True),
             _release("v0.4.1-beta.1")]
    assert str(upd.pick_release(items).version) == "0.4.0"
    assert str(upd.pick_release(items, prereleases=True).version) == "0.5.0rc1"
    assert upd.pick_release([]) is None and upd.pick_release({"message": "Not Found"}) is None


def test_whats_new_is_plain_text() -> None:
    body = ("Intro\n\n## New in this version\n\n- **Bold** and *italic* with [a link](https://x.y) and `code`\n"
            "  that goes on\n- <img src=x onerror=alert(1)>\n\n## Install\n\n- not this\n")
    assert upd.whats_new(body) == "• Bold and italic with a link and code that goes on\n• <img src=x onerror=alert(1)>"
    assert upd.whats_new("## Something else\n- x") == ""
    assert upd.whats_new("### Nouveautés\r\n\r\n1. Une\r\n2. Deux") == "• Une\n• Deux"
    long = "### New in this version\n" + "\n".join(f"- line {i} " + "x" * 60 for i in range(100))
    out = upd.whats_new(long)
    assert len(out) <= upd.NOTES_MAX + 2 and out.endswith("\n…")


# ------------------------------------------------------------------------------- the feed


class Feed:
    """A local stand-in for GitHub's API and its downloads."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[bytes | Path, str, str | None]] = {}
        self.server = upd._feed_server(self.routes)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.dmg = b"pretend disk image " * 4000
        self.sha = hashlib.sha256(self.dmg).hexdigest()
        self.routes["/dmg"] = (self.dmg, "application/octet-stream", None)

    def release(self, tag: str = "v0.4.0", digest: str | None = None, **kw: Any) -> dict[str, Any]:
        data = _release(tag, digest=f"sha256:{digest or self.sha}", size=len(self.dmg), **kw)
        data["assets"][1]["browser_download_url"] = f"{self.base}/dmg"
        return data

    def publish(self, path: str, data: Any, etag: str | None = '"v1"') -> str:
        self.routes[path] = (json.dumps(data).encode(), "application/json", etag)
        return self.base + path

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def feed() -> Iterator[Feed]:
    f = Feed()
    yield f
    f.close()


def test_check_with_etag_and_not_modified(feed: Feed) -> None:
    url = feed.publish("/latest", feed.release())
    first = upd.fetch_release(url, local=True)
    assert first.status == "ok" and first.etag == '"v1"' and str(first.release.version) == "0.4.0"
    assert upd.fetch_release(url, first.etag, local=True).status == "not_modified"
    assert upd.fetch_release(feed.base + "/nothing", local=True).status == "none"
    # only GitHub over HTTPS, unless it is the self-test's own local server
    assert upd.fetch_release(url).status == "error"
    assert upd.allowed_url(upd.LATEST_URL) and upd.allowed_url("https://objects.githubusercontent.com/x")
    assert not upd.allowed_url("http://api.github.com/x") and not upd.allowed_url("https://github.com.evil.example/x")
    assert not upd.allowed_url("https://example.com/x", local=True) and upd.allowed_url("http://127.0.0.1:9/x", local=True)


def test_rate_limits_and_being_offline_are_quiet(feed: Feed, monkeypatch: pytest.MonkeyPatch) -> None:
    import http.server

    class Limited(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(403)
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("X-RateLimit-Reset", "4102444800")  # 2100
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Limited)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        res = upd.fetch_release(f"http://127.0.0.1:{server.server_address[1]}/x", local=True, now=lambda: 1000.0)
        assert res.status == "rate_limited" and res.retry_at == 4102444800.0
    finally:
        server.shutdown()
        server.server_close()
    port = feed.server.server_address[1]
    feed.close()  # nobody answers any more: offline
    res = upd.fetch_release(f"http://127.0.0.1:{port}/latest", local=True, timeout=2)
    assert res.status == "offline"
    feed.server = upd._feed_server({})  # for the fixture's close


# ------------------------------------------------------------------------------- download


def test_download_is_verified(feed: Feed, tmp_path: Path) -> None:
    good = upd.parse_release(feed.release())
    seen: list[tuple[int, int]] = []
    path = upd.download(good.asset, tmp_path / "dl", on_progress=lambda d, t: seen.append((d, t)), local=True)
    assert path.read_bytes() == feed.dmg and seen[-1] == (len(feed.dmg), len(feed.dmg))
    assert upd.verify_file(path, good.asset) and not (tmp_path / "dl" / (path.name + ".part")).exists()


@pytest.mark.parametrize("case", ["wrong digest", "no digest", "wrong size", "elsewhere", "cancelled"])
def test_a_download_that_does_not_match_is_refused_and_deleted(feed: Feed, tmp_path: Path, case: str) -> None:
    asset = upd.parse_release(feed.release()).asset
    cancel = threading.Event()
    if case == "wrong digest":
        asset = upd.Asset(asset.name, asset.url, asset.size, "0" * 64)
    elif case == "no digest":
        asset = upd.Asset(asset.name, asset.url, asset.size, None)
    elif case == "wrong size":
        asset = upd.Asset(asset.name, asset.url, asset.size + 1, asset.sha256)
    elif case == "elsewhere":
        asset = upd.Asset(asset.name, "https://example.com/Voicestream.dmg", asset.size, asset.sha256)
    else:
        cancel.set()
    with pytest.raises(upd.UpdateError) as err:
        upd.download(asset, tmp_path, cancel=cancel, local=True)
    code = {"wrong digest": "digest_mismatch", "no digest": "no_digest", "wrong size": "size_mismatch",
            "elsewhere": "bad_source", "cancelled": "cancelled"}[case]
    assert err.value.code == code
    assert list(tmp_path.iterdir()) == []  # nothing kept, not even a half download


def test_a_redirect_must_stay_on_github(feed: Feed, tmp_path: Path) -> None:
    import http.server

    class Away(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "https://example.com/evil.dmg")
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Away)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        asset = upd.Asset("Voicestream-0.4.0-arm64.dmg", f"http://127.0.0.1:{server.server_address[1]}/d", 10, "0" * 64)
        with pytest.raises(upd.UpdateError) as err:
            upd.download(asset, tmp_path, local=True)
        assert err.value.code == "bad_source"
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------------- the new bundle


def _macho(cpus: list[int]) -> bytes:
    if len(cpus) == 1:
        return struct.pack("<II", 0xFEEDFACF, cpus[0]) + b"\0" * 24
    head = struct.pack(">II", 0xCAFEBABE, len(cpus))
    return head + b"".join(struct.pack(">iiIII", c, 0, 0, 0, 0) for c in cpus)


def _bundle(folder: Path, version: str = "0.4.0", bundle_id: str = upd.BUNDLE_ID, cpus: list[int] | None = None) -> Path:
    app = folder / "Voicestream.app"
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version, "CFBundleExecutable": "Voicestream"}))
    (app / "Contents" / "MacOS" / "Voicestream").write_bytes(_macho(cpus or [upd.ARM64]))
    return app


class FakeTools:
    """hdiutil, codesign, ditto and xattr as a Mac answers them, recorded."""

    def __init__(self, volume: Path, *, codesign_ok: bool = True) -> None:
        self.volume, self.codesign_ok, self.calls = volume, codesign_ok, []

    def __call__(self, args: list[str], timeout: float = 0) -> subprocess.CompletedProcess:
        self.calls.append(args)
        tool, out, code = Path(args[0]).name, "", 0
        if tool == "hdiutil" and args[1] == "attach":
            out = plistlib.dumps({"system-entities": [{"dev-entry": "/dev/disk9"},
                                                      {"mount-point": str(self.volume)}]}).decode()
        elif tool == "codesign":
            code = 0 if self.codesign_ok else 1
        elif tool == "ditto":
            shutil.copytree(args[-2], args[-1], symlinks=True)
        elif tool == "xattr":
            code = 1  # no such attribute
        return subprocess.CompletedProcess(args, code, out, "" if code == 0 else "invalid signature")


def test_macho_architectures(tmp_path: Path) -> None:
    for cpus, want in (([upd.ARM64], {"arm64"}), ([upd.X86_64], {"x86_64"}), ([upd.X86_64, upd.ARM64], {"x86_64", "arm64"})):
        (tmp_path / "bin").write_bytes(_macho(cpus))
        assert upd.macho_archs(tmp_path / "bin") == want
    (tmp_path / "bin").write_bytes(b"#!/bin/sh\n")
    assert upd.macho_archs(tmp_path / "bin") == set()


def test_bundle_checks(tmp_path: Path) -> None:
    tools = FakeTools(tmp_path)
    assert upd.bundle_problems(_bundle(tmp_path / "a"), "0.4.0", tools) == []
    assert ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(tmp_path / "a" / "Voicestream.app")] in tools.calls
    assert upd.bundle_problems(_bundle(tmp_path / "b", version="0.3.9"), "0.4.0", tools) == [
        "its version is '0.3.9', not 0.4.0"]
    assert "it is another app ('com.example.other')" in upd.bundle_problems(
        _bundle(tmp_path / "c", bundle_id="com.example.other"), "0.4.0", tools)
    assert upd.bundle_problems(_bundle(tmp_path / "d", cpus=[upd.X86_64]), "0.4.0", tools) == [
        "it has no code for Apple Silicon (arm64)"]
    assert upd.bundle_problems(_bundle(tmp_path / "e"), "0.4.0", FakeTools(tmp_path, codesign_ok=False))[0].startswith(
        "its signature is broken")
    assert upd.bundle_problems(tmp_path / "missing.app", "0.4.0", tools) == ["missing.app is not an app"]


def test_where_the_running_app_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert upd.install_problem(None)[0] == "not_packaged"
    translocated = Path("/private/var/folders/x/AppTranslocation/ABC/d/Voicestream.app")
    assert upd.install_problem(translocated)[0] == "translocated"
    assert upd.install_problem(Path("/Volumes/Voicestream/Voicestream.app"))[0] == "on_dmg"
    app = _bundle(tmp_path / "Applications")
    assert upd.install_problem(app) is None
    monkeypatch.setattr(upd.os, "access", lambda p, mode: Path(p) != app.parent)
    assert upd.install_problem(app)[0] == "not_writable"
    assert upd.running_app() is None  # not the packaged app


def test_app_management_is_probed_with_a_harmless_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _bundle(tmp_path)
    assert upd.app_management_blocked(app) is False
    assert sorted(p.name for p in (app / "Contents").iterdir()) == ["Info.plist", "MacOS"]  # nothing left behind

    def refuse(*a: Any, **kw: Any) -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(upd.os, "open", refuse)
    assert upd.app_management_blocked(app) is True


# --------------------------------------------------------------------------- state machine


def _r(version: str) -> upd.Release:
    return upd.parse_release(_release(f"v{version}"))


def test_the_update_state_machine() -> None:
    m = upd.UpdateMachine(current=V("0.3.0"))
    assert m.phase == "idle" and not m.found(_r("0.3.0")) and not m.found(None)
    assert m.found(_r("0.4.0")) and m.phase == "available"
    assert not m.found(_r("0.4.0"))  # the same one again: nothing changes
    with pytest.raises(upd.InvalidTransition):
        m.installing()
    m.start_download()
    m.advance(10, 1234)
    assert m.view()["progress"] == {"done": 10, "total": 1234}
    assert not m.found(_r("0.5.0")) and str(m.release.version) == "0.4.0"  # the download under way finishes first
    m.downloaded()
    assert m.phase == "ready" and m.progress is None
    assert m.found(_r("0.5.0")) and m.phase == "available" and str(m.release.version) == "0.5.0"  # newer: start over
    m.start_download()
    m.fail("the download stopped", "download_failed")
    assert m.view()["phase"] == "failed" and m.view()["error_code"] == "download_failed"
    m.start_download()  # tried again
    m.downloaded()
    m.installing()
    assert m.phase == "installing" and not m.found(_r("0.6.0"))
    # a check that says there is nothing newer any more takes the offer back
    m2 = upd.UpdateMachine(current=V("0.3.0"))
    m2.found(_r("0.4.0"))
    assert m2.found(None) and m2.phase == "idle" and m2.release is None
    m2.found(_r("0.4.0"))
    m2.start_download()
    m2.downloaded()
    assert m2.found(_r("0.3.0")) and m2.phase == "idle" and m2.release is None  # downloaded, then pulled


def test_an_install_during_a_meeting_waits_for_it_to_end() -> None:
    m = upd.UpdateMachine(current=V("0.3.0"))
    with pytest.raises(upd.InvalidTransition):
        m.defer()
    m.found(_r("0.4.0"))
    m.defer()
    assert m.deferred and not m.offer
    m.start_download()
    m.downloaded()
    assert m.deferred and m.phase == "ready"  # downloaded meanwhile, not installed
    assert m.meeting_over() and m.offer and not m.deferred
    assert not m.meeting_over()  # said once
    m.installing()
    assert not m.offer


# ------------------------------------------------------------------------------ the helper


def test_the_helper_takes_every_path_as_an_argument() -> None:
    text = upd.HELPER
    assert text.startswith("#!/bin/sh\n") and "set -u" in text
    assert 'pid=$1 app=$2 new=$3 dmg=$4 result=$5 log=$6 relaunch=$7' in text
    # every variable is used quoted (none could split on a space or expand a glob)
    import re

    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"\$\{?[a-z_]+\}?", line):
            before = line[: m.start()]
            assert before.count('"') % 2 == 1 or "$((" in before, f"unquoted {m.group(0)} in: {line}"
    assert "/usr/bin/xattr" in text and "com.apple.quarantine" in text and "/usr/bin/open" in text
    assert "--verify --deep --strict" in text
    # the old version goes away only after the new one is in place
    assert text.index('"$mv" "$app" "$old"') < text.index('"$mv" "$new" "$app"') < text.index('"$rm" -rf "$old"')
    subprocess.run(["sh", "-n", "-c", text], check=True)


def test_the_helper_is_shellcheck_clean(tmp_path: Path) -> None:
    exe = shutil.which("shellcheck")
    if exe is None:
        pytest.skip("shellcheck is not installed")
    script = upd.write_helper(tmp_path)
    proc = subprocess.run([exe, "-s", "sh", str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout


def _stub_helper(folder: Path, fail_mv: int = 0, wait: int | None = None) -> tuple[Path, Path]:
    """The helper with macOS's tools swapped for stand-ins that log what they were asked (``fail_mv``: the
    mv call that fails, as App Management would refuse it)."""
    tools = folder / "tools"
    tools.mkdir()
    calls = folder / "calls.log"
    for name, body in {
        "open": f'printf "open %s\\n" "$1" >>"{calls}"',
        "xattr": f'printf "xattr %s\\n" "$*" >>"{calls}"',
        "codesign": f'printf "codesign %s\\n" "$*" >>"{calls}"',
        "mv": (f'n=$(cat "{folder}/mv.n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"{folder}/mv.n"; '
               f'if [ "$n" = {fail_mv} ]; then echo "mv: Operation not permitted" >&2; exit 1; fi; exec /bin/mv "$@"'),
    }.items():
        (tools / name).write_text(f"#!/bin/sh\n{body}\n")
        (tools / name).chmod(0o755)
    text = upd.HELPER
    for name in ("mv", "open", "xattr", "codesign"):
        old = next(line for line in text.splitlines() if line.startswith(f"{name}="))
        text = text.replace(old, f"{name}='{tools / name}'")
    if wait is not None:
        text = text.replace(f'-ge {int(upd.HELPER_WAIT_S * 2)} ]', f"-ge {wait} ]")
    script = folder / "update helper.sh"
    script.write_text(text)
    script.chmod(stat.S_IRWXU)
    return script, calls


DMG_NAME = "the update's \"dmg\".dmg"


def _run_helper(script: Path, folder: Path, app: Path, new: Path, *, relaunch: bool = True,
                pid: int | None = None) -> subprocess.Popen:
    stand_in = None
    if pid is None:
        stand_in = subprocess.Popen(["sleep", "0.3"])
        pid = stand_in.pid
    (folder / DMG_NAME).write_bytes(b"verified")
    helper = upd.start_helper(script, pid=pid, app=app, new=new, dmg=folder / DMG_NAME,
                              result=folder / "result", log_file=folder / "update log.txt", relaunch=relaunch)
    if stand_in is not None:
        stand_in.wait(5)
    helper.wait(30)
    return helper


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell")
def test_the_helper_swaps_the_app_in_a_folder_with_spaces_and_quotes(tmp_path: Path) -> None:
    folder = tmp_path / "Support folder"
    folder.mkdir()
    apps = tmp_path / "My Apps 'n' \"stuff\" $HOME"
    old = _bundle(apps, "0.3.0")
    new = upd.staged_path(old, "0.4.0")
    shutil.copytree(_bundle(tmp_path / "new", "0.4.0"), new)
    script, calls = _stub_helper(folder)
    helper = _run_helper(script, folder, old, new)
    assert helper.returncode == 0, (folder / "update log.txt").read_text()
    assert plistlib.loads((old / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"] == "0.4.0"
    assert sorted(p.name for p in apps.iterdir()) == ["Voicestream.app"]  # the old one gone, nothing staged left
    assert (folder / "result").read_text().strip() == "installed"
    said = calls.read_text().splitlines()
    assert said == [f"xattr -dr com.apple.quarantine {old}", f"codesign --verify --deep --strict {old}", f"open {old}"]
    assert "Voicestream quit: replacing" in (folder / "update log.txt").read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell")
@pytest.mark.parametrize(("fail_mv", "code"), [(1, "app_management"), (2, "move_failed")])
def test_a_failed_swap_puts_the_old_app_back_and_opens_the_update(tmp_path: Path, fail_mv: int, code: str) -> None:
    folder = tmp_path / "support"
    folder.mkdir()
    old = _bundle(tmp_path / "Applications", "0.3.0")
    new = upd.staged_path(old, "0.4.0")
    shutil.copytree(_bundle(tmp_path / "new", "0.4.0"), new)
    script, calls = _stub_helper(folder, fail_mv=fail_mv)
    helper = _run_helper(script, folder, old, new)
    assert helper.returncode == 1
    assert plistlib.loads((old / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"] == "0.3.0"
    assert sorted(p.name for p in old.parent.iterdir()) == ["Voicestream.app"]
    assert (folder / "result").read_text().startswith(f"failed: {code}: ")
    # the verified update open in Finder, and the old app open again: never left without one
    assert calls.read_text().splitlines() == [f"open {folder / DMG_NAME}", f"open {old}"]


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell")
def test_the_helper_gives_up_when_the_app_does_not_quit(tmp_path: Path) -> None:
    folder = tmp_path / "support"
    folder.mkdir()
    old = _bundle(tmp_path / "Applications", "0.3.0")
    new = upd.staged_path(old, "0.4.0")
    shutil.copytree(_bundle(tmp_path / "new", "0.4.0"), new)
    script, calls = _stub_helper(folder, wait=2)
    helper = _run_helper(script, folder, old, new, pid=os.getpid())  # this process: it won't quit
    assert helper.returncode == 1 and (folder / "result").read_text().startswith("failed: not_quit")
    assert not new.exists() and old.is_dir() and not calls.exists()


def test_the_helper_is_started_in_its_own_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Popen:
        def __init__(self, args: list[str], **kw: Any) -> None:
            seen.update(kw, args=args)

    monkeypatch.setattr(upd.subprocess, "Popen", Popen)
    upd.start_helper(tmp_path / "h.sh", pid=42, app=Path("/Applications/Voicestream.app"), new=Path("/x y/new.app"),
                     dmg=Path("/d.dmg"), result=Path("/r"), log_file=Path("/l"))
    assert seen["args"] == ["/bin/sh", str(tmp_path / "h.sh"), "42", "/Applications/Voicestream.app", "/x y/new.app",
                            "/d.dmg", "/r", "/l", "1"]
    assert seen["start_new_session"] is True and seen["close_fds"] is True and seen["stdin"] == subprocess.DEVNULL
    script = upd.write_helper(tmp_path / "support")
    assert script.read_text() == upd.HELPER and stat.S_IMODE(script.stat().st_mode) == 0o700


# ------------------------------------------------------------------- the host's updater


def _updater(tmp_path: Path, feed: Feed, busy: list[bool] | None = None) -> tuple[upd.Updater, list[int]]:
    changes: list[int] = []
    flag = busy if busy is not None else [False]
    u = upd.Updater(tmp_path / "updates", on_change=lambda: changes.append(1), busy=lambda: flag[0],
                    feed=feed.base + "/latest", local=True)
    u.machine.current = V("0.3.0")
    return u, changes


def test_checks_remember_the_etag_and_when(tmp_path: Path, feed: Feed) -> None:
    feed.publish("/latest", feed.release())
    u, changes = _updater(tmp_path, feed)
    assert u.due()
    assert asyncio.run(u.check()) == "available" and changes
    assert u.machine.phase == "available" and u.etag == '"v1"' and u.last_checked is not None
    assert not u.due()  # checked this launch, and not six hours ago
    assert asyncio.run(u.check()) == "available"  # through "not modified": the cached release
    saved = json.loads((tmp_path / "updates" / "state.json").read_text())
    assert saved["etag"] == '"v1"' and saved["release"]["version"] == "0.4.0"
    again = upd.Updater(tmp_path / "updates", on_change=lambda: None, busy=lambda: False)  # the next launch
    assert again.etag == '"v1"' and again.cached is not None and again.due()
    assert again.due(every=0)
    u.now = lambda: time.time() + upd.CHECK_EVERY_S + 1
    assert u.due()


def test_offline_and_up_to_date(tmp_path: Path, feed: Feed) -> None:
    feed.publish("/latest", feed.release("v0.3.0"))
    u, _ = _updater(tmp_path, feed)
    assert asyncio.run(u.check()) == "up_to_date" and u.machine.phase == "idle"
    u.feed = "http://127.0.0.1:9/latest"  # nobody there
    assert asyncio.run(u.check(manual=True)) == "offline"
    assert u.last_result == "offline" and not u.due()  # a while before the next quiet look


def test_install_downloads_checks_and_waits_for_the_meeting(tmp_path: Path, feed: Feed) -> None:
    feed.publish("/latest", feed.release())
    busy = [True]
    u, _ = _updater(tmp_path, feed, busy)
    quits: list[int] = []
    u.quit_app = lambda: quits.append(1)
    u.can_install = True

    async def run() -> None:
        await u.check()
        assert u.install(None) == "deferred"  # a meeting is recorded: downloaded, not installed
        await u._task
        assert u.machine.phase == "ready" and u.machine.deferred and quits == []
        assert (tmp_path / "updates" / "Voicestream-0.4.0-arm64.dmg").read_bytes() == feed.dmg
        busy[0] = False
        u.meeting_over()
        assert u.machine.offer and not u.machine.deferred
        # not the packaged app: refused, and said why (the window offers the download page)
        assert u.install(None) == "started"
        await u._task
        assert u.machine.phase == "failed" and u.machine.error_code == "not_packaged" and quits == []

    asyncio.run(run())


def test_install_prepares_the_swap_then_quits(tmp_path: Path, feed: Feed, monkeypatch: pytest.MonkeyPatch) -> None:
    feed.publish("/latest", feed.release())
    u, _ = _updater(tmp_path, feed)
    quits: list[int] = []
    u.quit_app = lambda: quits.append(1)
    u.can_install = True
    app = _bundle(tmp_path / "Applications", "0.3.0")
    volume = tmp_path / "volume"
    _bundle(volume, "0.4.0")
    tools = FakeTools(volume)
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(upd, "_run", tools)
    monkeypatch.setattr(upd.Updater._prepare, "__defaults__", (tools,))
    monkeypatch.setattr(upd, "start_helper", lambda script, **kw: started.append({"script": script, **kw}))

    async def run() -> None:
        await u.check()
        assert u.install(app) == "started"
        await u._task

    asyncio.run(run())
    assert u.machine.phase == "installing" and quits == [1], u.machine.error
    (call,) = started
    assert call["app"] == app and call["new"] == upd.staged_path(app, "0.4.0") and call["pid"] == os.getpid()
    assert (call["new"] / "Contents" / "Info.plist").is_file() and call["script"].read_text() == upd.HELPER
    attach = next(c for c in tools.calls if c[:2] == ["/usr/bin/hdiutil", "attach"])
    assert {"-readonly", "-nobrowse", "-noautoopen"} <= set(attach)
    assert any(c[:2] == ["/usr/bin/ditto", "--noqtn"] for c in tools.calls)
    assert any(c[:2] == ["/usr/bin/hdiutil", "detach"] for c in tools.calls)  # unmounted once staged


def _held_install(tmp_path: Path, feed: Feed, monkeypatch: pytest.MonkeyPatch) -> tuple[upd.Updater, Path, dict[str, Any]]:
    """An updater whose install stops while it stages the new app (``held["preparing"]``) until
    ``held["go_on"]`` is set; ``held["started"]``: the helpers started, ``held["quits"]``: the quits."""
    feed.publish("/latest", feed.release())
    u, _ = _updater(tmp_path, feed)
    held: dict[str, Any] = {"preparing": threading.Event(), "go_on": threading.Event(), "started": [], "quits": []}
    u.quit_app = lambda: held["quits"].append(1)
    u.can_install = True
    app = _bundle(tmp_path / "Applications", "0.3.0")
    volume = tmp_path / "volume"
    _bundle(volume, "0.4.0")
    tools = FakeTools(volume)
    monkeypatch.setattr(upd, "_run", tools)
    monkeypatch.setattr(upd, "start_helper", lambda script, **kw: held["started"].append(kw))
    real = upd.Updater._prepare

    def slow(self: upd.Updater, dmg: Path, app: Path, version: upd.Version, run: Any = tools) -> Path:
        held["preparing"].set()
        held["go_on"].wait(10)
        return real(self, dmg, app, version, run)

    monkeypatch.setattr(upd.Updater, "_prepare", slow)
    return u, app, held


async def _until_preparing(held: dict[str, Any]) -> None:
    for _ in range(500):
        if held["preparing"].is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the install never got to staging the new app")


def test_a_check_during_the_install_leaves_its_release_alone(tmp_path: Path, feed: Feed,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A newer release published while the update is being staged: the install goes on with the one it
    staged (the next check offers the newer one), rather than failing with its helper already waiting."""
    u, app, held = _held_install(tmp_path, feed, monkeypatch)

    async def run() -> None:
        await u.check()
        assert u.install(app) == "started"
        await _until_preparing(held)
        feed.publish("/latest", feed.release("v0.5.0"), etag='"v2"')
        assert await u.check() == "available"
        assert u.machine.phase == "ready" and str(u.machine.release.version) == "0.4.0"
        feed.publish("/latest", feed.release("v0.3.0"), etag='"v3"')  # or pulled: likewise
        assert await u.check() == "up_to_date"
        assert u.machine.phase == "ready" and str(u.machine.release.version) == "0.4.0"
        held["go_on"].set()
        await u._task

    asyncio.run(run())
    assert u.machine.phase == "installing" and held["quits"] == [1], u.machine.error
    (call,) = held["started"]
    assert call["new"] == upd.staged_path(app, "0.4.0")


def test_no_helper_is_started_for_a_release_no_longer_on_offer(tmp_path: Path, feed: Feed,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    u, app, held = _held_install(tmp_path, feed, monkeypatch)

    async def run() -> None:
        await u.check()
        assert u.install(app) == "started"
        await _until_preparing(held)
        u.machine.found(_r("0.5.0"))  # however the release on offer changed meanwhile
        held["go_on"].set()
        await u._task

    asyncio.run(run())
    assert held["started"] == [] and held["quits"] == []  # no helper waiting to swap in what nobody installs
    assert u.machine.phase == "available" and str(u.machine.release.version) == "0.5.0"
    assert not upd.staged_path(app, "0.4.0").exists() and u.staged is None


def test_a_downloaded_release_that_was_pulled_is_no_longer_offered(tmp_path: Path, feed: Feed) -> None:
    feed.publish("/latest", feed.release())
    u, _ = _updater(tmp_path, feed, [True])  # in a meeting: downloaded, not installed

    async def run() -> None:
        await u.check()
        assert u.install(None) == "deferred"
        await u._task
        assert u.machine.phase == "ready" and u.dmg is not None and u.dmg.is_file()
        dmg = u.dmg
        u.staged = tmp_path / "Applications" / ".Voicestream-0.4.0.update.app"
        (u.staged / "Contents").mkdir(parents=True)
        staged = u.staged
        feed.publish("/latest", feed.release("v0.3.0"), etag='"v2"')  # the maintainer took 0.4.0 back
        assert await u.check() == "up_to_date"
        assert u.machine.phase == "idle" and u.machine.release is None and not u.machine.deferred
        assert not dmg.exists() and not staged.exists() and u.dmg is None and u.staged is None

    asyncio.run(run())


def test_app_management_opens_the_update_in_finder_instead(tmp_path: Path, feed: Feed,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    feed.publish("/latest", feed.release())
    u, _ = _updater(tmp_path, feed)
    u.quit_app = lambda: pytest.fail("must not quit")
    u.can_install = True
    opened: list[Path] = []
    monkeypatch.setattr(upd, "app_management_blocked", lambda app: True)
    monkeypatch.setattr(upd, "open_in_finder", opened.append)
    app = _bundle(tmp_path / "Applications", "0.3.0")

    async def run() -> None:
        await u.check()
        u.install(app)
        await u._task

    asyncio.run(run())
    assert u.machine.phase == "failed" and u.machine.error_code == "app_management"
    assert opened == [tmp_path / "updates" / "Voicestream-0.4.0-arm64.dmg"]
    assert sorted(p.name for p in app.parent.iterdir()) == ["Voicestream.app"]  # nothing staged


def test_what_the_helper_did_is_said_at_the_next_launch(tmp_path: Path) -> None:
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / "result").write_text("failed: app_management: macOS did not let the old version be moved\n")
    (folder / "Voicestream-0.0.1-arm64.dmg").write_bytes(b"old")  # older than this version: tidied away
    (folder / "Voicestream-99.0.0-arm64.dmg.part").write_bytes(b"half")
    u = upd.Updater(folder, on_change=lambda: None, busy=lambda: False)
    assert u.outcome == {"kind": "failed", "detail": "app_management"} and not (folder / "result").exists()
    assert sorted(p.name for p in folder.iterdir()) == []
    assert u.put_off() and u.outcome is None
    (folder / "result").write_text("installed\n")
    assert upd.Updater(folder, on_change=lambda: None, busy=lambda: False).outcome == {"kind": "installed", "detail": None}


# ------------------------------------------------------------------------- host and routes


@pytest.fixture
def host(tmp_path: Path, feed: Feed) -> AppHost:
    h = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json",
                assistant_factory=_factory(), update_dir=tmp_path / "updates")
    h.updater.feed, h.updater.local = feed.base + "/latest", True
    h.updater.machine.current = V("0.3.0")
    return h


@pytest.fixture
def client(host: AppHost) -> Iterator[TestClient]:
    with TestClient(create_app(host, TOKEN), base_url=BASE, headers={TOKEN_HEADER: TOKEN}) as c:
        yield c


def test_the_update_routes(client: TestClient, host: AppHost, feed: Feed) -> None:
    feed.publish("/latest", feed.release())
    state = client.get("/api/app").json()
    assert state["update"]["phase"] == "idle" and state["update"]["current"] == "0.3.0"
    assert state["prefs"]["check_updates"] is True and state["prefs"]["update_prereleases"] is False
    assert state["capabilities"]["self_update"] is False and state["update"]["can_install"] is False
    body = client.post("/api/update/check").json()
    assert body["result"] == "available" and body["update"]["release"]["version"] == "0.4.0"
    assert body["update"]["release"]["notes"].startswith("• Meetings start when you say so.")
    assert client.get("/api/update").json()["phase"] == "available"
    assert client.post("/api/update/later").json()["update"]["later"] == "0.4.0"
    assert client.post("/api/update/cancel").json()["result"] is False  # nothing downloading
    # not the packaged app: the install says why, and nothing quits
    assert client.post("/api/update/install").json()["result"] == "started"
    deadline = time.monotonic() + 10
    while client.get("/api/update").json()["phase"] != "failed":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert client.get("/api/update").json()["error_code"] == "not_packaged"
    for path in ("/api/update", "/api/update/check", "/api/update/install"):
        with TestClient(create_app(host, TOKEN), base_url=BASE) as anonymous:
            assert anonymous.request("GET" if path == "/api/update" else "POST", path).status_code == 401
    assert client.patch("/api/prefs", json={"check_updates": False}).json()["check_updates"] is False
    assert client.patch("/api/prefs", json={"check_updates": "yes please"}).status_code == 400


def test_the_host_never_installs_during_a_meeting(host: AppHost) -> None:
    host.prefs = host.prefs.model_copy(update={"meeting_start": "manual"})
    assert not host.update_busy()
    for status in ("starting", "listening", "paused", "stopping"):
        host.status = status
        assert host.update_busy(), status
    host.prefs = host.prefs.model_copy(update={"meeting_start": "automatic"})
    host.status = "listening"
    assert not host.update_busy()  # always-on listening between meetings: it starts again after the restart
    host._start_queued = True
    assert host.update_busy()
    host._start_queued = False
    host._retiring[object()] = None  # type: ignore[index]  # an earlier meeting's agents, still at work
    assert host.update_busy()


def _deferred(host: AppHost) -> upd.UpdateMachine:
    """Install was asked for during the meeting: downloaded, put off until it is over."""
    m = host.updater.machine
    m.current = V("0.3.0")
    m.found(_r("0.4.0"))
    m.start_download()
    m.downloaded()
    m.defer()
    return m


async def test_an_update_waits_for_an_automatic_meeting_s_wrap_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Always-on: a meeting closed on a silence is wrapped up in the background while listening goes on.
    Offered as it closed, Install would quit mid-summary: the notes bare, the pushes skipped."""
    from helpers import seg
    from test_app_host import _host, _until
    from test_app_meeting_start import Wall, _clocked, _events, _slow_wrap_up

    gate = _slow_wrap_up(monkeypatch)
    wall = Wall()
    built: list[Any] = []
    host = _host(tmp_path, factory=_clocked(wall, built), mode="automatic")  # meetings close after 10 min
    try:
        await host.listen_start()
        await built[0]._route(seg("Let's start.", "mic", t=1.0))
        assert host.update_busy()
        m = _deferred(host)
        wall.advance(11)
        await _until(lambda: bool(_events(host, "meeting.ending")))
        await asyncio.sleep(0.1)
        assert not _events(host, "meeting.ended") and not host.in_meeting() and host.status == "listening"
        assert host.update_busy() and m.deferred and not m.offer  # its summary and pushes are still to come
        gate.set()
        await _until(lambda: bool(_events(host, "meeting.ended")))
        assert not host.update_busy() and m.offer and not m.deferred  # offered once its notes are written
    finally:
        gate.set()
        await host.shutdown()


async def test_an_update_waits_for_a_meeting_started_by_hand_to_be_wrapped_up(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from helpers import seg
    from test_app_host import _until
    from test_app_meeting_start import _events, _manual, _slow_wrap_up

    gate = _slow_wrap_up(monkeypatch)
    built: list[Any] = []
    host = _manual(tmp_path, built=built)
    m = _deferred(host)
    try:
        await host.meeting_start()
        await built[0]._route(seg("I'll send the deck to Sam by Friday.", "mic", t=1.0))
        assert await host.meeting_stop() == "stop"
        await _until(lambda: bool(_events(host, "meeting.ending")))
        await asyncio.sleep(0.1)
        assert host.update_busy() and m.deferred and not m.offer
        gate.set()
        await _until(lambda: host.status == "stopped")
        assert _events(host, "meeting.ended") and not host.update_busy() and m.offer
    finally:
        gate.set()
        await host.shutdown()


def test_the_automatic_check_waits_for_the_setup_and_the_preference(tmp_path: Path, feed: Feed,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    feed.publish("/latest", feed.release())
    h = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json",
                assistant_factory=_factory(), update_dir=tmp_path / "updates", auto_updates=True)
    h.updater.feed, h.updater.local = feed.base + "/latest", True
    h.updater.machine.current = V("0.3.0")
    monkeypatch.setattr("voicestream.app.host.FIRST_CHECK_DELAY_S", 0.0)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fast(s: float) -> None:
        sleeps.append(s)
        await real_sleep(0.01)

    async def run() -> None:
        monkeypatch.setattr("voicestream.app.host.asyncio.sleep", fast)
        task = asyncio.ensure_future(h._update_loop())
        while len(sleeps) < 3:
            await real_sleep(0.01)
        assert h.updater.last_checked is None  # the setup isn't done: no look yet
        h.prefs = h.prefs.model_copy(update={"setup_done": True, "check_updates": False})
        n = len(sleeps)
        while len(sleeps) < n + 3:
            await real_sleep(0.01)
        assert h.updater.last_checked is None  # turned off
        h.prefs = h.prefs.model_copy(update={"check_updates": True})
        for _ in range(500):
            if h.updater.machine.phase == "available":
                break
            await real_sleep(0.01)
        h._closing = True
        task.cancel()

    asyncio.run(run())
    assert h.updater.machine.phase == "available"


# ------------------------------------------------------------------------------- self-test


def test_tls_certificates_and_no_proxy_for_the_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    assert upd.tls_context().cert_store_stats()["x509_ca"] > 0  # what GitHub's certificate is checked with
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    local = upd._opener(True)
    assert not any(isinstance(h, upd.urllib.request.ProxyHandler) and h.proxies for h in local.handlers)


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell")
def test_the_packaged_apps_update_selftest_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the macOS CI's self-test runs (updater.selftest), here with macOS's tools as stand-ins: a local
    feed, a refused wrong digest, a verified download, the mounted app checked, the helper's swap into a copy
    of the app — which then is the new version."""
    dmg = tmp_path / "Voicestream-99.0.0-arm64.dmg"
    dmg.write_bytes(b"a disk image " * 5000)
    volume = tmp_path / "volume"
    _bundle(volume, "99.0.0")
    running = _bundle(tmp_path / "running", __version__)
    tools = FakeTools(volume)
    script, _calls = _stub_helper(tmp_path)
    monkeypatch.setattr(upd, "HELPER", script.read_text())  # the helper with stand-ins for mv, codesign, xattr, open
    monkeypatch.setattr(upd, "_read_only", lambda path: path == volume)  # hdiutil's -readonly mount
    work = tmp_path / "work"
    work.mkdir()
    out = upd.selftest(dmg, running, work, tools)
    assert out["checks"] == dict.fromkeys(upd.SELFTEST_CHECKS, True), out
    assert out["version"] == "99.0.0" and out["swap"]["version"] == "99.0.0" and out["swap"]["result"] == "installed"
    assert out["wrong_digest"].startswith("refused") and out["download"]["quarantined"] is False
    assert out["check"]["again"] == "not_modified" and out["check"]["tls_certificates"] > 0
    assert out["install_problem"] is None and out["app_management_blocked"] is False
    # the app it runs as was copied, never touched
    assert plistlib.loads((running / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"] == __version__


def test_the_selftest_says_when_it_has_no_dmg(tmp_path: Path) -> None:
    out = upd.selftest(tmp_path / "nothing.dmg", None, tmp_path)
    assert out["checks"] == {} and "no test DMG" in out["error"]


# ------------------------------------------------------------------------- the menu bar


def test_the_menu_bar_offers_the_update() -> None:
    from voicestream.app import native

    def state(phase: str, can: bool = True) -> dict[str, Any]:
        return {"state": "stopped", "update": {"phase": phase, "can_install": can,
                                               "release": {"version": "0.4.0"} if phase != "idle" else None}}

    entry = native.update_entry(state("available"), "en")
    assert (entry.title, entry.visible, entry.enabled) == ("Install Update 0.4.0…", True, True)
    assert native.update_entry(state("available"), "fr").title == "Installer la mise à jour 0.4.0…"
    assert not native.update_entry(state("idle"), "en").visible
    assert not native.update_entry(state("available", can=False), "en").visible  # not the packaged app
    down = native.update_entry(state("downloading"), "en")
    assert (down.title, down.enabled) == ("Downloading Update 0.4.0…", False)
    assert not native.update_entry(state("available"), "en", quitting=True).enabled
    keys = [e.key for e in native.menu_model(state("available"), "en", "disabled")]
    assert keys == [e.key for e in native.menu_model(state("idle"), "en", "disabled")]  # built once, updated in place
    assert keys.index("update") == keys.index("open") + 1
