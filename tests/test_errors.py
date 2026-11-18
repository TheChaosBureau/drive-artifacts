# SPDX-License-Identifier: Apache-2.0
import re
import tomllib
from pathlib import Path

import pytest

from drive_artifacts import __version__
from drive_artifacts.errors import (
    AuthError,
    DriveArtifactsError,
    DriveError,
    IntegrityError,
    NoFilesError,
    NotFoundError,
    UsageError,
)

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)


def test_pyproject_and_package_versions_agree() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev\d+)?", __version__)


def test_changelog_has_a_section_for_a_released_version() -> None:
    # The release workflow greps for this heading on the tag; fail here, not on the tag.
    if ".dev" in __version__:
        pytest.skip("a development version has no changelog section yet")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(
        rf"^## \[{re.escape(__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE
    )


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (UsageError("x"), 2),
        (AuthError("x"), 3),
        (NotFoundError("x"), 4),
        (DriveError("x", status=503, reason="backendError"), 5),
        (IntegrityError("x"), 6),
        (NoFilesError("x"), 7),
    ],
)
def test_exit_codes(exc: DriveArtifactsError, code: int) -> None:
    assert exc.exit_code == code
    assert isinstance(exc, DriveArtifactsError)


def test_drive_error_carries_status_and_reason() -> None:
    err = DriveError("rate limited", status=403, reason="userRateLimitExceeded")
    assert (err.status, err.reason) == (403, "userRateLimitExceeded")
