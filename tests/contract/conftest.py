# SPDX-License-Identifier: Apache-2.0
"""Fixtures for tests that talk to the real Drive API. Everything lands under one throwaway root folder."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from drive_artifacts.drive import DriveClient, DriveFile
from drive_artifacts.errors import DriveArtifactsError
from drive_artifacts.model import FOLDER_MIME, Repo, RunContext, prop, query
from drive_artifacts.oauth import RefreshingTokenSource, StoredToken
from drive_artifacts.settings import Settings
from drive_artifacts.store import ArtifactStore

CONTRACT_ROOT = re.compile(r"GitHub Artifacts \(contract [0-9a-f]{8}\)")
STALE_AFTER = timedelta(days=1)


def stale_contract_roots(folders: Sequence[DriveFile], now: datetime) -> list[DriveFile]:
    """The throwaway roots of earlier sessions that never reached their teardown (a killed
    runner, a cancelled job). Only folders this suite named, and only once they are more
    than a day old: a root of a session that is still running is never touched."""
    stale: list[DriveFile] = []
    for folder in folders:
        if CONTRACT_ROOT.fullmatch(folder.name) is None:
            continue
        try:
            created = datetime.fromisoformat(folder.created_time)
        except ValueError:
            continue  # no usable date: leave it alone
        if created.tzinfo is not None and now - created > STALE_AFTER:
            stale.append(folder)
    return stale


@pytest.fixture(scope="session")
def real_settings() -> Settings:
    if not os.environ.get("DRIVE_ARTIFACTS_TOKEN"):
        pytest.skip("DRIVE_ARTIFACTS_TOKEN is not set; contract tests need a real grant")
    root = f"GitHub Artifacts (contract {uuid.uuid4().hex[:8]})"
    return Settings.from_env({**os.environ, "DRIVE_ARTIFACTS_ROOT": root})


@pytest.fixture(scope="session")
def real_drive(real_settings: Settings) -> Iterator[DriveClient]:
    with httpx.Client() as http:
        tokens = RefreshingTokenSource(
            http, real_settings, StoredToken.parse(real_settings.token_text or "")
        )
        drive = DriveClient(http, tokens, real_settings)
        # Housekeeping, never a reason to fail the session: sweep what earlier sessions left.
        with suppress(DriveArtifactsError):
            roots = drive.list_files(query(prop("da.kind", "root"), f"mimeType = '{FOLDER_MIME}'"))
            for leftover in stale_contract_roots(roots, datetime.now(UTC)):
                drive.trash(leftover.id)
        yield drive


@pytest.fixture(scope="session")
def real_store(real_drive: DriveClient, real_settings: Settings) -> Iterator[ArtifactStore]:
    store = ArtifactStore(real_drive, root_name=real_settings.root_name)
    yield store
    root = store.root_folder(create=False)
    if root is not None:  # trash the throwaway root; Drive trashes its contents with it
        real_drive.trash(root.id)


@pytest.fixture
def real_ctx() -> RunContext:
    return RunContext(
        repo=Repo(
            id=os.environ.get("GITHUB_REPOSITORY_ID", "0"),
            slug=os.environ.get("GITHUB_REPOSITORY", "local/contract"),
        ),
        run_id=os.environ.get("GITHUB_RUN_ID", uuid.uuid4().hex[:10]),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        ref="refs/heads/contract",
        commit="0" * 40,
    )
