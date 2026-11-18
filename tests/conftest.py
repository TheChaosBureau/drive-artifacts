# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import date

import httpx
import pytest

from drive_artifacts.drive import DriveClient, RetryPolicy
from drive_artifacts.model import Repo, RunContext
from drive_artifacts.oauth import RefreshingTokenSource, StoredToken
from drive_artifacts.settings import Settings
from drive_artifacts.store import ArtifactStore
from tests.fake_drive import FakeDrive

TOKEN_JSON = StoredToken(refresh_token="rt-1", client_id="cid").dumps()


@pytest.fixture
def fake() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env(
        {
            "DRIVE_ARTIFACTS_CLIENT_ID": "cid",
            "DRIVE_ARTIFACTS_CLIENT_SECRET": "csec",
            "DRIVE_ARTIFACTS_TOKEN": TOKEN_JSON,
        }
    )


@pytest.fixture
def http(fake: FakeDrive) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


@pytest.fixture
def tokens(http: httpx.Client, settings: Settings) -> RefreshingTokenSource:
    return RefreshingTokenSource(
        http, settings, StoredToken.parse(TOKEN_JSON), sleep=lambda _s: None
    )


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def drive(
    http: httpx.Client, tokens: RefreshingTokenSource, settings: Settings, sleeps: list[float]
) -> DriveClient:
    return DriveClient(
        http,
        tokens,
        settings,
        policy=RetryPolicy(max_attempts=5, deadline_s=60.0),
        sleep=sleeps.append,
        rng=lambda: 1.0,
        chunk_size=1024,
    )


@pytest.fixture
def store(drive: DriveClient) -> ArtifactStore:
    return ArtifactStore(drive, root_name="GitHub Artifacts", today=lambda: date(2026, 10, 11))


@pytest.fixture
def ctx() -> RunContext:
    return RunContext(
        repo=Repo(id="987", slug="TheChaosBureau/drive-artifacts"),
        run_id="12345",
        run_attempt="1",
        ref="refs/heads/main",
        commit="a" * 40,
    )
