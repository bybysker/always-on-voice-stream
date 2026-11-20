"""Releasing a version (packaging/release.py and .github/workflows/release.yml): checked, then written."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("voicestream_release", ROOT / "packaging" / "release.py")
assert _spec is not None and _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release  # its dataclass looks its module up there
_spec.loader.exec_module(release)

PYPROJECT = '[project]\nname = "voicestream"\nversion = "{v}"\ndescription = "x"\n\n[tool.other]\nversion = "9.9.9"\n'
INIT = '"""voicestream."""\n\n__version__ = "{v}"\n'
NOTES = "<!-- release notes for {v} -->\nIntro\n\n### New in this version\n\n- **Summaries** to send.\n\n### Install\n\n1. Open it.\n"


def _repo(tmp_path: Path, current: str = "0.4.0-beta.1", notes_for: str = "0.4.0", init: str | None = None,
          notes: str = NOTES) -> Path:
    (tmp_path / "src" / "voicestream").mkdir(parents=True)
    (tmp_path / "packaging" / "mac").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT.format(v=current), encoding="utf-8")
    (tmp_path / "src" / "voicestream" / "__init__.py").write_text(INIT.format(v=init or current), encoding="utf-8")
    (tmp_path / "packaging" / "mac" / "release-notes.md").write_text(notes.format(v=notes_for), encoding="utf-8")
    return tmp_path


def test_a_version_is_checked_then_written_where_the_app_reads_it(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    r = release.check(root, "v0.4.0", tags=["v0.3.0", "v0.4.0-beta.1"])
    assert (str(r.version), r.tag, r.prerelease) == ("0.4.0", "v0.4.0", False)
    release.write(root, r)
    assert (root / "pyproject.toml").read_text() == PYPROJECT.format(v="0.4.0").replace('"9.9.9"', '"9.9.9"')
    assert '[tool.other]\nversion = "9.9.9"' in (root / "pyproject.toml").read_text()  # only the project's version
    assert (root / "src" / "voicestream" / "__init__.py").read_text() == INIT.format(v="0.4.0")
    assert str(release.current(root)) == "0.4.0"


def test_a_suffix_makes_a_pre_release(tmp_path: Path) -> None:
    root = _repo(tmp_path, current="0.4.0", notes_for="v0.5.0-beta.1")
    r = release.check(root, "0.5.0-beta.1", tags=[])
    assert (r.tag, r.prerelease) == ("v0.5.0-beta.1", True)


@pytest.mark.parametrize(("wanted", "message"), [
    ("0.4", "isn't a version"), ("latest", "isn't a version"), ("0.4.0 beta", "isn't a version"),
    ("0.4.0;rm -rf /", "isn't a version"),
    ("0.3.9", "0.3.9 isn't newer than 0.4.0-beta.1: the app would never offer it"),
    ("0.4.0-beta.1", "isn't newer"),
    ("0.4.0b1", "isn't a version"),  # PEP 440's spelling: the release job's tag pattern refuses it
])
def test_a_version_the_app_wouldnt_offer_is_refused(tmp_path: Path, wanted: str, message: str) -> None:
    with pytest.raises(release.ReleaseError, match=re.escape(message)):
        release.check(_repo(tmp_path), wanted, tags=[])


def test_a_release_needs_its_own_notes_and_no_tag_yet(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError, match="v0.4.0 is released already"):
        release.check(_repo(tmp_path / "a"), "0.4.0", tags=["v0.4.0"])
    with pytest.raises(release.ReleaseError, match=r"are the notes for 0.3.0: write the ones for 0.4.0 first"):
        release.check(_repo(tmp_path / "b", notes_for="0.3.0"), "0.4.0", tags=[])
    with pytest.raises(release.ReleaseError, match="must start with <!-- release notes for 0.4.0 -->"):
        release.check(_repo(tmp_path / "c", notes="Intro\n### New in this version\n- x\n"), "0.4.0", tags=[])
    with pytest.raises(release.ReleaseError, match='no "### New in this version" section'):
        release.check(_repo(tmp_path / "d", notes="<!-- release notes for {v} -->\n### Install\n- x\n"), "0.4.0", tags=[])
    too_long = "<!-- release notes for {v} -->\n### New in this version\n" + "\n".join(f"- point {i} " + "x" * 80 for i in range(30))
    with pytest.raises(release.ReleaseError, match="longer than the banner shows"):
        release.check(_repo(tmp_path / "e", notes=too_long), "0.4.0", tags=[])
    with pytest.raises(release.ReleaseError, match="pyproject.toml says 0.4.0-beta.1 and src/voicestream/__init__.py 0.3.0"):
        release.check(_repo(tmp_path / "f", init="0.3.0"), "0.4.0", tags=[])


def test_the_command_prints_what_the_workflow_uses_and_check_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                             capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(release, "git_tags", lambda root: ["v0.3.0"])
    assert release.main(["0.4.0", "--check"], root=root) == 0
    assert capsys.readouterr().out == "version=0.4.0\ntag=v0.4.0\nprerelease=false\n"
    assert str(release.current(root)) == "0.4.0-beta.1"
    assert release.main(["0.4.0"], root=root) == 0 and str(release.current(root)) == "0.4.0"
    capsys.readouterr()
    assert release.main(["0.4.0"], root=root) == 1  # now it is the current one
    assert capsys.readouterr().err.startswith("::error::0.4.0 isn't newer than 0.4.0")


def test_the_repository_is_ready_for_the_version_its_notes_are_for() -> None:
    """The notes are for this version or the next one; for the next, everything but its tag is ready."""
    current, notes = release.current(ROOT), release.parse_version(release.notes_version(ROOT))
    assert notes is not None and notes >= current
    if notes > current:
        assert release.check(ROOT, str(notes), tags=[]).tag == f"v{notes}"


def test_the_release_workflow_tags_main_and_starts_the_mac_build() -> None:
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    on = wf[wf.index("\non:"):wf.index("\npermissions:")]
    assert "workflow_dispatch:" in on and "push" not in on  # by hand only: a person decides a release
    assert "permissions:\n  contents: read" in wf
    assert "      contents: write" in wf and "      actions: write" in wf
    assert "if: github.ref == 'refs/heads/main'" in wf
    assert 'python packaging/release.py "$VERSION" >> "$GITHUB_OUTPUT"' in wf
    assert 'git push --atomic origin HEAD:refs/heads/main "refs/tags/$TAG"' in wf
    # a tag pushed with the job's token starts no workflow: the Mac build is started on it explicitly
    assert 'gh workflow run mac-app.yml --ref "$TAG" -f release="$TAG"' in wf
    # what was typed reaches the scripts through the environment only, never as ${{ }} inside them
    for line in wf.splitlines():
        if "${{" in line:
            assert re.match(r"^\s+[A-Z_]+: \$\{\{ [^}]+ \}\}", line), line
    # the release job publishes what this accepts, and nothing else
    mac = (ROOT / ".github" / "workflows" / "mac-app.yml").read_text(encoding="utf-8")
    assert release.TAG.pattern in mac and "startsWith(github.ref, 'refs/tags/v')" in mac
