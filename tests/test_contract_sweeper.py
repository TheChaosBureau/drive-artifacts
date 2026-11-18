# SPDX-License-Identifier: Apache-2.0
"""The rule that picks which leftover contract-test folders to trash, checked offline."""

from datetime import UTC, datetime

from drive_artifacts.drive import DriveFile
from tests.contract.conftest import stale_contract_roots

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _folder(name: str, created: str, *, kind: str = "root") -> DriveFile:
    return DriveFile(
        id=f"id-{name}-{created}",
        name=name,
        mime_type="application/vnd.google-apps.folder",
        parents=(),
        app_properties={"da.kind": kind},
        size=None,
        created_time=created,
        modified_time=created,
        web_view_link="",
        trashed=False,
    )


def test_only_contract_roots_older_than_a_day_are_stale() -> None:
    old = _folder("GitHub Artifacts (contract 0123abcd)", "2026-11-18T04:17:00.000Z")
    fresh = _folder("GitHub Artifacts (contract 89abcdef)", "2026-11-20T11:59:00.000Z")
    real = _folder("GitHub Artifacts", "2026-01-01T00:00:00.000Z")  # the user's own root: never
    custom = _folder("My CI", "2026-01-01T00:00:00.000Z")
    lookalike = _folder("GitHub Artifacts (contract nothex!!)", "2026-01-01T00:00:00.000Z")
    undated = _folder("GitHub Artifacts (contract 0123abcd)", "")
    garbled = _folder("GitHub Artifacts (contract 0123abcd)", "yesterday")
    stale = stale_contract_roots([old, fresh, real, custom, lookalike, undated, garbled], NOW)
    assert stale == [old]


def test_a_root_exactly_a_day_old_is_left_for_the_next_run() -> None:
    edge = _folder("GitHub Artifacts (contract 0123abcd)", "2026-11-19T12:00:00.000Z")
    assert stale_contract_roots([edge], NOW) == []
    older = _folder("GitHub Artifacts (contract 0123abcd)", "2026-11-19T11:59:59.000Z")
    assert stale_contract_roots([older], NOW) == [older]
