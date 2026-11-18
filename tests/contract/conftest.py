# SPDX-License-Identifier: Apache-2.0
"""Fixtures for tests that talk to the real Drive API. Everything lands under one throwaway root folder."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest

from drive_artifacts.drive import DriveClient
from drive_artifacts.model import Repo, RunContext
from drive_artifacts.oauth import RefreshingTokenSource, StoredToken
from drive_artifacts.settings import Settings
from drive_artifacts.store import ArtifactStore


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
        yield DriveClient(http, tokens, real_settings)


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
