# SPDX-License-Identifier: Apache-2.0
"""The release workflow's last step, run for real with a stand-in for `gh`."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None,
    reason="runs the step under bash with a POSIX shell stand-in for gh",
)

CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

- Not out yet.

## [0.2.0] - 2027-01-02

### Fixed

- The second thing.

## [0.1.0] - 2026-11-19

### Added

- The first thing.

[Unreleased]: https://example.test/compare/v0.2.0...HEAD
[0.2.0]: https://example.test/releases/tag/v0.2.0
[0.1.0]: https://example.test/releases/tag/v0.1.0
"""

# Logs every call. `release view` fails (no release yet); the exact-ref probe answers by
# $STUB_MAJOR_EXISTS; the matching-refs listing prints $STUB_TAGS, one ref per line.
GH = """\
#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1 $2" in
  "release view") exit 1 ;;
  "release create") exit 0 ;;
esac
case "$*" in
  *git/matching-refs/tags/*) printf '%s\\n' $STUB_TAGS; exit 0 ;;
  *"-X PATCH"*|*"-X POST"*) exit 0 ;;
  *git/ref/tags/*) [ "$STUB_MAJOR_EXISTS" = yes ] && exit 0 || exit 1 ;;
esac
exit 0
"""


def _release_step() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    (step,) = (
        s for s in workflow["jobs"]["publish"]["steps"] if "gh release" in str(s.get("run", ""))
    )
    return str(step["run"])


def _run(
    tmp_path: Path, tag: str, *, tags: list[str], major_exists: bool
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    assert BASH is not None
    work = tmp_path / "work"
    (work / "dist").mkdir(parents=True)
    (work / "dist" / "pkg.whl").write_bytes(b"wheel")
    (work / "CHANGELOG.md").write_text(CHANGELOG)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    log.write_text("")
    script = tmp_path / "step.sh"
    script.write_text(_release_step())
    result = subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=work,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "unused",
            "TAG": tag,
            "REPO": "o/r",
            "SHA": "f" * 40,
            "STUB_LOG": str(log),
            "STUB_TAGS": " ".join(f"refs/tags/{t}" for t in tags),
            "STUB_MAJOR_EXISTS": "yes" if major_exists else "no",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log.read_text().splitlines(), (work / "notes.md").read_text()


def _moves(calls: list[str]) -> list[str]:
    return [c for c in calls if "-X PATCH" in c or "-X POST" in c]


def test_the_release_notes_are_one_changelog_section_without_the_link_lines(
    tmp_path: Path,
) -> None:
    result, calls, notes = _run(tmp_path, "v0.2.0", tags=["v0.1.0", "v0.2.0"], major_exists=True)
    assert result.returncode == 0, result.stderr
    assert notes == "## [0.2.0] - 2027-01-02\n\n### Fixed\n\n- The second thing.\n\n"
    assert any(c.startswith("release create v0.2.0 dist/pkg.whl") for c in calls)
    result, _, notes = _run(tmp_path / "last", "v0.1.0", tags=["v0.1.0"], major_exists=False)
    assert result.returncode == 0, result.stderr
    assert notes == "## [0.1.0] - 2026-11-19\n\n### Added\n\n- The first thing.\n\n"


def test_the_first_release_of_a_major_creates_its_moving_tag(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, "v0.1.0", tags=["v0.1.0"], major_exists=False)
    assert result.returncode == 0, result.stderr
    (move,) = _moves(calls)
    assert "-X POST repos/o/r/git/refs" in move
    assert "ref=refs/tags/v0" in move
    assert f"sha={'f' * 40}" in move


def test_a_newer_release_moves_the_major_tag_forward(tmp_path: Path) -> None:
    result, calls, _ = _run(
        tmp_path, "v0.10.0", tags=["v0.1.0", "v0.9.0", "v0.10.0"], major_exists=True
    )
    assert result.returncode == 0, result.stderr
    (move,) = _moves(calls)
    assert "-X PATCH repos/o/r/git/refs/tags/v0" in move
    assert f"sha={'f' * 40}" in move


def test_re_running_an_older_release_never_moves_the_major_tag_backwards(tmp_path: Path) -> None:
    """`v0` is what `uses: ...@v0` resolves to: a re-run of the 0.9.0 release must not take
    every caller back from 0.10.0."""
    result, calls, _ = _run(
        tmp_path, "v0.9.0", tags=["v0.1.0", "v0.9.0", "v0.10.0"], major_exists=True
    )
    assert result.returncode == 0, result.stderr
    assert _moves(calls) == []
    assert "::notice::v0 stays at v0.10.0" in result.stdout


def test_an_empty_tag_listing_does_not_stop_the_major_tag_from_being_created(
    tmp_path: Path,
) -> None:
    """Under `pipefail` a `grep` that matches nothing would fail the step after the release
    was already published."""
    result, calls, _ = _run(tmp_path, "v1.0.0", tags=[], major_exists=False)
    assert result.returncode == 0, result.stderr
    (move,) = _moves(calls)
    assert "ref=refs/tags/v1" in move
