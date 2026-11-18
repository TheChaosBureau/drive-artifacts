# SPDX-License-Identifier: Apache-2.0
"""What only the real Drive API can prove: queries, resumable uploads, ranged downloads, error shapes."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from drive_artifacts.archive import build_zip, resolve_paths, safe_extract, sha256_file
from drive_artifacts.drive import DriveClient, DriveFile
from drive_artifacts.errors import DriveError, IntegrityError, NotFoundError
from drive_artifacts.model import ArtifactKey, RunContext
from drive_artifacts.oauth import RefreshingTokenSource, StoredToken
from drive_artifacts.settings import Settings
from drive_artifacts.store import ArtifactStore

pytestmark = pytest.mark.contract


def _tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"alpha")
    # 750 KiB: compressible and nested, but still one chunk; the big test crosses chunk boundaries
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 3000)
    return src


def test_round_trip_find_latest_and_prune(
    real_store: ArtifactStore, real_ctx: RunContext, tmp_path: Path
) -> None:
    src = _tree(tmp_path)
    built = build_zip(resolve_paths(["."], src), tmp_path / "a.zip")
    record = real_store.put(
        real_ctx, "contract", built.path, sha256=built.sha256, size=built.size, retention_days=1
    )
    assert record.size == built.size
    assert record.sha256 == built.sha256
    key = ArtifactKey(real_ctx.repo.id, real_ctx.run_id, real_ctx.run_attempt, "contract")
    found = real_store.find(key)
    assert found is not None
    assert found.file_id == record.file_id
    latest = real_store.latest(real_ctx.repo.id, "contract", ref="refs/heads/contract")
    assert latest is not None
    assert latest.file_id == record.file_id
    out = tmp_path / "out.zip"
    real_store.fetch(record, out)
    assert sha256_file(out) == built.sha256
    extracted = tmp_path / "x"
    safe_extract(out, extracted)
    assert (extracted / "a.txt").read_bytes() == b"alpha"
    assert (extracted / "sub" / "b.bin").read_bytes() == bytes(range(256)) * 3000
    with pytest.raises(DriveError, match="already exists"):
        real_store.put(
            real_ctx, "contract", built.path, sha256=built.sha256, size=built.size, retention_days=1
        )
    assert real_store.prune(real_ctx.repo.id) == []  # retention 1 day: not expired yet
    assert real_store.prune(real_ctx.repo.id, dry_run=True) == []


def test_large_upload_uses_several_chunks(
    real_store: ArtifactStore, real_ctx: RunContext, tmp_path: Path
) -> None:
    big = tmp_path / "big"
    big.mkdir()
    # 20 MiB is more than two 8 MiB chunks; random bytes so the zip does not shrink below that
    (big / "blob").write_bytes(os.urandom(20 * 1024 * 1024))
    built = build_zip(resolve_paths(["."], big), tmp_path / "big.zip", compression_level=0)
    record = real_store.put(
        real_ctx, "big", built.path, sha256=built.sha256, size=built.size, retention_days=1
    )
    assert record.size == built.size
    out = tmp_path / "big-out.zip"
    real_store.fetch(record, out)
    assert sha256_file(out) == built.sha256


def test_missing_and_tampered(
    real_store: ArtifactStore,
    real_drive: DriveClient,
    real_ctx: RunContext,
    tmp_path: Path,
) -> None:
    assert real_store.find(ArtifactKey(real_ctx.repo.id, "no-such-run", "1", "x")) is None
    with pytest.raises(NotFoundError):
        real_drive.get_file("definitely-not-a-file-id")
    src = _tree(tmp_path)
    built = build_zip(resolve_paths(["a.txt"], src), tmp_path / "t.zip")
    record = real_store.put(
        real_ctx, "tamper", built.path, sha256=built.sha256, size=built.size, retention_days=1
    )
    dest = tmp_path / "bad.zip"
    with pytest.raises(IntegrityError):
        real_store.fetch(replace(record, sha256="0" * 64), dest)
    assert not dest.exists()  # a download that fails its digest is not left behind


PROBE = bytes(range(250)) * 4


def _probe(
    real_store: ArtifactStore, real_drive: DriveClient, tmp_path: Path, name: str
) -> tuple[DriveFile, str]:
    """Upload 1000 known bytes straight through the client, under the throwaway root."""
    root = real_store.root_folder(create=True)
    assert root is not None
    blob = tmp_path / name
    blob.write_bytes(PROBE)
    uploaded = real_drive.upload_file(
        blob, name=name, parent=root.id, app_properties={"da.probe": name}
    )
    return uploaded, root.id


def test_final_chunk_answer_carries_the_requested_fields(
    real_store: ArtifactStore, real_drive: DriveClient, tmp_path: Path
) -> None:
    """The `fields` on the session's opening POST reach the answer to the last chunk, so
    `ArtifactStore.put` needs no second read. If Google stops doing that, `put` still works
    (it re-reads the file) and this test says why every upload costs one more request."""
    uploaded, root_id = _probe(real_store, real_drive, tmp_path, "fields-probe")
    assert uploaded.size == len(PROBE)
    assert uploaded.created_time
    assert uploaded.web_view_link
    assert uploaded.app_properties == {"da.probe": "fields-probe"}
    assert uploaded.parents == (root_id,)


def test_ranged_download_answers_206_with_content_range(
    real_store: ArtifactStore, real_drive: DriveClient, real_settings: Settings, tmp_path: Path
) -> None:
    """A resumed download sends `Range: bytes=N-` and appends only on a 206."""
    uploaded, _ = _probe(real_store, real_drive, tmp_path, "range-probe")
    with httpx.Client() as http:
        tokens = RefreshingTokenSource(
            http, real_settings, StoredToken.parse(real_settings.token_text or "")
        )
        response = http.get(
            f"{real_settings.api_base}/files/{uploaded.id}",
            params={"alt": "media"},
            headers={"Range": "bytes=400-", "Authorization": f"Bearer {tokens.access_token()}"},
            timeout=60,
        )
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 400-999/1000"
    assert response.content == PROBE[400:]


@pytest.mark.usefixtures("real_store")  # its teardown also trashes the root the subprocess creates
def test_cli_against_real_drive(
    real_settings: Settings, real_ctx: RunContext, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    (work / "dist").mkdir(parents=True)
    (work / "dist" / "wheel.whl").write_bytes(b"not really a wheel")
    env = {
        **os.environ,
        "DRIVE_ARTIFACTS_ROOT": real_settings.root_name,
        "GITHUB_REPOSITORY_ID": real_ctx.repo.id,
        "GITHUB_REPOSITORY": real_ctx.repo.slug,
        "GITHUB_RUN_ID": real_ctx.run_id,
        "GITHUB_RUN_ATTEMPT": real_ctx.run_attempt,
        "GITHUB_REF": real_ctx.ref,
        "GITHUB_SHA": real_ctx.commit,
    }
    cli = [sys.executable, "-m", "drive_artifacts"]
    up = subprocess.run(
        [*cli, "upload", "--name", "cli", "--path", "dist/", "--retention-days", "1"],
        env=env,
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert up.returncode == 0, up.stderr
    down = subprocess.run(
        [*cli, "download", "--name", "cli", "--path", "back"],
        env=env,
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert down.returncode == 0, down.stderr
    assert (work / "back" / "wheel.whl").read_bytes() == b"not really a wheel"
