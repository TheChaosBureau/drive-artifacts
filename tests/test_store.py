# SPDX-License-Identifier: Apache-2.0
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from drive_artifacts.archive import sha256_file
from drive_artifacts.drive import DriveClient, DriveFile
from drive_artifacts.errors import DriveError, IntegrityError, UsageError
from drive_artifacts.model import FOLDER_MIME, ArtifactKey, ArtifactRecord, Repo, RunContext
from drive_artifacts.store import ArtifactStore
from tests.fake_drive import FakeDrive


def _zip(tmp_path: Path, name: str, payload: bytes) -> tuple[Path, str, int]:
    p = tmp_path / name
    p.write_bytes(payload)
    return p, sha256_file(p), len(payload)


def _found(record: ArtifactRecord | None) -> ArtifactRecord:
    assert record is not None
    return record


def _patch(fake: FakeDrive, file_id: str, body: dict[str, object]) -> None:
    """A metadata change made behind the store's back (it moves modifiedTime, never createdTime)."""
    c = httpx.Client(
        transport=httpx.MockTransport(fake.handler), headers={"Authorization": "Bearer at-1"}
    )
    response = c.patch(f"https://www.googleapis.com/drive/v3/files/{file_id}", json=body)
    assert response.status_code == 200


def _add_duplicate(fake: FakeDrive, parent: str, sha256: str) -> str:
    """A second live file for the key (987, 12345, 1, wheel), as a lost create response leaves."""
    props = {
        "da.v": "1",
        "da.kind": "artifact",
        "da.repo": "987",
        "da.run": "12345",
        "da.attempt": "1",
        "da.name": "wheel",
        "da.sha256": sha256,
        "da.ref": "refs/heads/main",
        "da.commit": "a" * 40,
        "da.expires": "2026-12-31",
    }
    return fake.add_file("12345-1-wheel.zip", b"dup", parent=parent, props=props)


def test_put_creates_the_layout_and_the_record(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"wheel bytes")
    rec = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=30)
    assert rec.key == ArtifactKey("987", "12345", "1", "wheel")
    assert (rec.sha256, rec.size, rec.ref, rec.commit) == (
        digest,
        size,
        "refs/heads/main",
        "a" * 40,
    )
    assert rec.expires == date(2026, 11, 10)
    file = fake.files[rec.file_id]
    assert file.name == "12345-1-wheel.zip"
    assert file.content == b"wheel bytes"
    repo_folder = fake.files[file.parents[0]]
    assert repo_folder.name == "TheChaosBureau/drive-artifacts"
    assert repo_folder.app_properties == {"da.v": "1", "da.kind": "repo", "da.repo": "987"}
    root = fake.files[repo_folder.parents[0]]
    assert root.name == "GitHub Artifacts"
    assert root.app_properties == {"da.v": "1", "da.kind": "root"}
    assert root.parents == []


def test_folders_are_reused_across_puts_and_repos(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    store.put(ctx, "a", path, sha256=digest, size=size, retention_days=1)
    store.put(ctx, "b", path, sha256=digest, size=size, retention_days=1)
    other = replace(ctx, repo=Repo(id="1", slug="o/r"))
    store.put(other, "a", path, sha256=digest, size=size, retention_days=1)
    folders = [f for f in fake.live_files() if f.mime_type == FOLDER_MIME]
    assert sorted(f.name for f in folders) == [
        "GitHub Artifacts",
        "TheChaosBureau/drive-artifacts",
        "o/r",
    ]


def test_duplicate_name_conflicts_unless_overwrite(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    first = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    with pytest.raises(DriveError, match="already exists") as info:
        store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    assert info.value.reason == "conflict"
    path2, digest2, size2 = _zip(tmp_path, "w2.zip", b"two")
    second = store.put(
        ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True
    )
    assert fake.files[first.file_id].trashed
    assert not fake.files[second.file_id].trashed
    assert _found(store.find(first.key)).file_id == second.file_id


def test_validation(store: ArtifactStore, ctx: RunContext, tmp_path: Path) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    with pytest.raises(UsageError):
        store.put(ctx, "bad:name", path, sha256=digest, size=size, retention_days=1)
    for days in (0, 401):
        with pytest.raises(UsageError, match="retention"):
            store.put(ctx, "ok", path, sha256=digest, size=size, retention_days=days)


def test_find_list_and_latest(store: ArtifactStore, ctx: RunContext, tmp_path: Path) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    store.put(ctx, "sdist", path, sha256=digest, size=size, retention_days=1)
    later = replace(ctx, run_id="12346", commit="b" * 40)
    newest = store.put(later, "wheel", path, sha256=digest, size=size, retention_days=1)
    feature = replace(ctx, run_id="12347", ref="refs/heads/feature", commit="c" * 40)
    on_feature = store.put(feature, "wheel", path, sha256=digest, size=size, retention_days=1)
    assert store.find(ArtifactKey("987", "12345", "1", "wheel")) is not None
    assert store.find(ArtifactKey("987", "12345", "1", "nope")) is None
    assert [r.key.name for r in store.list_run("987", "12345")] == ["sdist", "wheel"]
    assert [r.key.name for r in store.list_run("987", "12345", run_attempt="2")] == []
    assert _found(store.latest("987", "wheel")).file_id == on_feature.file_id
    assert _found(store.latest("987", "wheel", ref="refs/heads/main")).file_id == newest.file_id
    assert _found(store.latest("987", "wheel", commit="a" * 40)).key.run_id == "12345"
    assert store.latest("987", "missing") is None
    assert [r.key.run_id for r in store.list_repo("987")] == ["12347", "12346", "12345", "12345"]
    assert [r.key.run_id for r in store.list_repo("987", ref="refs/heads/feature")] == ["12347"]
    long_ref = "refs/heads/" + "renovate/" * 30  # far past the 117-byte property budget
    on_long = store.put(
        replace(ctx, run_id="12348", ref=long_ref),
        "wheel",
        path,
        sha256=digest,
        size=size,
        retention_days=1,
    )
    assert _found(store.latest("987", "wheel", ref=long_ref)).file_id == on_long.file_id
    assert [r.key.run_id for r in store.list_repo("987", ref=long_ref)] == ["12348"]
    assert on_long.ref.startswith("sha256:")


def test_fetch_verifies_the_digest(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"payload")
    rec = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    out = tmp_path / "dl" / "w.zip"
    store.fetch(rec, out)
    assert out.read_bytes() == b"payload"
    fake.files[rec.file_id].content = b"tampere"  # same length, different bytes
    with pytest.raises(IntegrityError):
        store.fetch(rec, tmp_path / "dl2" / "w.zip")
    assert not (tmp_path / "dl2" / "w.zip").exists()


def test_prune_trashes_only_expired_artifacts_of_the_repo(
    store: ArtifactStore, drive: DriveClient, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    old = ArtifactStore(drive, root_name="GitHub Artifacts", today=lambda: date(2026, 1, 1))
    expired = old.put(ctx, "old", path, sha256=digest, size=size, retention_days=10)
    fresh = store.put(ctx, "fresh", path, sha256=digest, size=size, retention_days=10)
    other = old.put(
        replace(ctx, repo=Repo(id="1", slug="o/r")),
        "old",
        path,
        sha256=digest,
        size=size,
        retention_days=10,
    )
    dry = store.prune("987", dry_run=True)
    assert [r.file_id for r in dry] == [expired.file_id]
    assert not fake.files[expired.file_id].trashed
    assert [r.file_id for r in store.prune("987")] == [expired.file_id]
    assert fake.files[expired.file_id].trashed
    assert not fake.files[fresh.file_id].trashed
    assert not fake.files[other.file_id].trashed


def test_folder_race_keeps_the_oldest_and_trashes_its_own(
    store: ArtifactStore, drive: DriveClient, fake: FakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = drive.create_folder

    def racing_create(
        name: str, *, parent: str | None, app_properties: dict[str, str]
    ) -> DriveFile:
        fake.add_folder(name, parent=parent, props=dict(app_properties))  # another job wins
        return real(name, parent=parent, app_properties=app_properties)

    monkeypatch.setattr(drive, "create_folder", racing_create)
    root = store.root_folder(create=True)
    assert root is not None
    roots = [f for f in fake.files.values() if f.app_properties.get("da.kind") == "root"]
    assert len(roots) == 2
    winner = min(roots, key=lambda f: f.created_time)
    assert root.id == winner.id
    assert [f.trashed for f in roots if f.id != winner.id] == [True]


def test_missing_folders_without_create_return_none(store: ArtifactStore) -> None:
    assert store.root_folder(create=False) is None
    assert store.repo_folder(Repo("1", "o/r"), create=False) is None


def test_repo_folder_race_keeps_the_oldest_too(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = store.root_folder(create=True)
    assert root is not None
    real = drive.create_folder

    def racing_create(
        name: str, *, parent: str | None, app_properties: dict[str, str]
    ) -> DriveFile:
        fake.add_folder(name, parent=parent, props=dict(app_properties))  # another job wins
        return real(name, parent=parent, app_properties=app_properties)

    monkeypatch.setattr(drive, "create_folder", racing_create)
    folder = store.repo_folder(ctx.repo, create=True)
    assert folder is not None
    repos = [f for f in fake.files.values() if f.app_properties.get("da.kind") == "repo"]
    assert len(repos) == 2
    winner = min(repos, key=lambda f: f.created_time)
    assert folder.id == winner.id
    assert [f.trashed for f in repos if f.id != winner.id] == [True]
    live_roots = [f.id for f in fake.live_files() if f.app_properties.get("da.kind") == "root"]
    assert live_roots == [root.id]


def test_overwrite_trashes_the_old_file_only_after_the_new_upload_is_complete(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    first = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    real_trash = drive.trash
    live_when_trashing: list[int] = []

    def watching_trash(file_id: str) -> None:
        live = [f for f in fake.live_files() if f.app_properties.get("da.kind") == "artifact"]
        live_when_trashing.append(len(live))
        real_trash(file_id)

    monkeypatch.setattr(drive, "trash", watching_trash)
    path2, digest2, size2 = _zip(tmp_path, "w2.zip", b"two!")
    second = store.put(
        ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True
    )
    # both the old file and the complete new one were live at the moment the old one was trashed
    assert live_when_trashing == [2]
    assert fake.files[first.file_id].trashed
    assert fake.files[second.file_id].content == b"two!"


def test_a_failed_overwrite_upload_keeps_the_old_artifact(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    first = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)

    def failing_upload(*_args: object, **_kwargs: object) -> DriveFile:
        raise DriveError("upload failed", status=500)

    monkeypatch.setattr(drive, "upload_file", failing_upload)
    path2, digest2, size2 = _zip(tmp_path, "w2.zip", b"two")
    with pytest.raises(DriveError, match="upload failed"):
        store.put(ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True)
    assert not fake.files[first.file_id].trashed
    assert _found(store.find(first.key)).file_id == first.file_id


def _lie_about_size_once(drive: DriveClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The next upload comes back from Drive with a size one byte too large."""
    real = drive.upload_file
    lie_next = [True]

    def upload(*args: Any, **kwargs: Any) -> DriveFile:
        uploaded = real(*args, **kwargs)
        if lie_next[0]:
            lie_next[0] = False
            return replace(uploaded, size=(uploaded.size or 0) + 1)
        return uploaded

    monkeypatch.setattr(drive, "upload_file", upload)


def test_a_size_that_disagrees_with_drive_trashes_the_upload_and_a_retry_succeeds(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"payload")
    _lie_about_size_once(drive, monkeypatch)
    with pytest.raises(DriveError, match="bytes") as info:
        store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    assert info.value.reason == "sizeMismatch"
    artifacts = [f for f in fake.files.values() if f.app_properties.get("da.kind") == "artifact"]
    assert [f.trashed for f in artifacts] == [True]  # no half-good artifact is left in Drive
    assert store.find(ArtifactKey("987", "12345", "1", "wheel")) is None
    retry = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)  # no conflict
    assert not fake.files[retry.file_id].trashed
    assert _found(store.find(retry.key)).file_id == retry.file_id


def test_a_size_mismatch_on_overwrite_leaves_the_old_artifact_live(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    first = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    path2, digest2, size2 = _zip(tmp_path, "w2.zip", b"two!")
    _lie_about_size_once(drive, monkeypatch)
    with pytest.raises(DriveError) as info:
        store.put(ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True)
    assert info.value.reason == "sizeMismatch"
    assert not fake.files[first.file_id].trashed  # the old artifact was not given up
    assert _found(store.find(first.key)).file_id == first.file_id
    live = [f for f in fake.live_files() if f.app_properties.get("da.kind") == "artifact"]
    assert [f.id for f in live] == [first.file_id]
    second = store.put(
        ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True
    )
    assert fake.files[first.file_id].trashed
    assert _found(store.find(first.key)).file_id == second.file_id


def test_a_failure_to_trash_the_mismatched_upload_does_not_mask_the_size_error(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"payload")
    _lie_about_size_once(drive, monkeypatch)

    def failing_trash(file_id: str) -> None:
        raise DriveError("trash failed", status=500)

    monkeypatch.setattr(drive, "trash", failing_trash)
    with pytest.raises(DriveError) as info:
        store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    assert info.value.reason == "sizeMismatch"


def test_the_default_clock_is_utc_today(
    drive: DriveClient, ctx: RunContext, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    before = datetime.now(UTC).date()
    rec = ArtifactStore(drive, root_name="GitHub Artifacts").put(
        ctx, "wheel", path, sha256=digest, size=size, retention_days=2
    )
    after = datetime.now(UTC).date()
    assert before + timedelta(days=2) <= rec.expires <= after + timedelta(days=2)


def test_listings_skip_files_that_are_not_valid_artifacts(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    good = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    parent = fake.files[good.file_id].parents[0]
    base = {"da.kind": "artifact", "da.repo": "987", "da.run": "12345", "da.attempt": "1"}
    future = {**base, "da.v": "2", "da.name": "wheel"}
    broken = {**base, "da.v": "1", "da.name": "wheel", "da.expires": "not-a-date"}
    fake.add_file("future.zip", b"x", parent=parent, props=future)
    fake.add_file("broken.zip", b"x", parent=parent, props=broken)
    assert [r.file_id for r in store.list_run("987", "12345")] == [good.file_id]
    assert [r.file_id for r in store.list_repo("987")] == [good.file_id]
    assert _found(store.find(good.key)).file_id == good.file_id
    assert _found(store.latest("987", "wheel")).file_id == good.file_id
    assert store.prune("987") == []


def test_latest_and_list_repo_follow_upload_order_not_metadata_changes(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    first = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    second = store.put(
        replace(ctx, run_id="99999"), "wheel", path, sha256=digest, size=size, retention_days=1
    )
    _patch(fake, first.file_id, {"trashed": True})
    _patch(
        fake, first.file_id, {"trashed": False}
    )  # modifiedTime of the older upload is now newest
    assert fake.files[first.file_id].modified_time > fake.files[second.file_id].modified_time
    assert _found(store.latest("987", "wheel")).file_id == second.file_id
    assert [r.key.run_id for r in store.list_repo("987")] == ["99999", "12345"]
    assert _found(store.latest("987", "wheel", ref="refs/heads/main")).file_id == second.file_id


def test_find_and_list_run_follow_upload_order_among_duplicates(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    original = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    parent = fake.files[original.file_id].parents[0]
    older_dup = _add_duplicate(fake, parent, digest)
    newest_dup = _add_duplicate(fake, parent, digest)
    _patch(fake, original.file_id, {"name": "touched.zip"})  # the oldest upload was modified last
    assert _found(store.find(original.key)).file_id == newest_dup
    runs = store.list_run("987", "12345")
    assert [r.file_id for r in runs] == [original.file_id, older_dup, newest_dup]


def test_a_losing_racer_keeps_its_folder_when_something_was_uploaded_into_it(
    store: ArtifactStore, drive: DriveClient, fake: FakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = drive.create_folder
    created: list[DriveFile] = []

    def racing_create(
        name: str, *, parent: str | None, app_properties: dict[str, str]
    ) -> DriveFile:
        fake.add_folder(name, parent=parent, props=dict(app_properties))  # another job wins
        mine = real(name, parent=parent, app_properties=app_properties)
        fake.add_file("late.zip", b"x", parent=mine.id)  # and a third job already used ours
        created.append(mine)
        return mine

    monkeypatch.setattr(drive, "create_folder", racing_create)
    root = store.root_folder(create=True)
    assert root is not None
    (mine,) = created
    assert root.id != mine.id  # the oldest folder still wins
    assert not fake.files[mine.id].trashed  # but the one holding a file is not thrown away


def test_overwrite_trashes_every_older_live_record_for_the_key(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    original = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    parent = fake.files[original.file_id].parents[0]
    dup1 = _add_duplicate(fake, parent, digest)
    dup2 = _add_duplicate(fake, parent, digest)
    path2, digest2, size2 = _zip(tmp_path, "w2.zip", b"two!")
    new = store.put(
        ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True
    )
    for stale in (original.file_id, dup1, dup2):
        assert fake.files[stale].trashed
    assert not fake.files[new.file_id].trashed
    assert [r.file_id for r in store.list_run("987", "12345")] == [new.file_id]
    assert _found(store.find(new.key)).file_id == new.file_id


def test_prune_keeps_what_expires_today_or_later_and_trashes_what_expired_yesterday(
    store: ArtifactStore, drive: DriveClient, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"x")

    def put(name: str, uploaded_on: date, days: int) -> ArtifactRecord:
        earlier = ArtifactStore(drive, root_name="GitHub Artifacts", today=lambda: uploaded_on)
        return earlier.put(ctx, name, path, sha256=digest, size=size, retention_days=days)

    today = date(2026, 10, 11)  # the `store` fixture's clock
    yesterday_expiry = put("expired", today - timedelta(days=2), 1)
    today_expiry = put("today", today - timedelta(days=1), 1)
    tomorrow_expiry = put("tomorrow", today - timedelta(days=1), 2)
    assert (yesterday_expiry.expires, today_expiry.expires, tomorrow_expiry.expires) == (
        today - timedelta(days=1),
        today,
        today + timedelta(days=1),
    )
    assert [r.file_id for r in store.prune("987", dry_run=True)] == [yesterday_expiry.file_id]
    assert [r.file_id for r in store.prune("987")] == [yesterday_expiry.file_id]
    assert fake.files[yesterday_expiry.file_id].trashed
    assert not fake.files[today_expiry.file_id].trashed
    assert not fake.files[tomorrow_expiry.file_id].trashed


@pytest.mark.parametrize("planted", ["..", ".", "../x", "/etc/cron.d", "x/../..", "ev\x00il"])
def test_a_record_with_a_planted_path_as_its_name_appears_in_no_listing(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path, planted: str
) -> None:
    """The name becomes a directory on download-all, so a record that could not have been
    stored by `put` is skipped by every query, like any other malformed file."""
    path, digest, size = _zip(tmp_path, "w.zip", b"x")
    good = store.put(ctx, "good", path, sha256=digest, size=size, retention_days=1)
    evil = store.put(ctx, "evil", path, sha256=digest, size=size, retention_days=1)
    fake.files[evil.file_id].app_properties["da.name"] = planted
    assert [r.file_id for r in store.list_run("987", "12345")] == [good.file_id]
    assert [r.file_id for r in store.list_repo("987")] == [good.file_id]
    assert [r.file_id for r in store.list_repo("987", ref="refs/heads/main")] == [good.file_id]
    assert store.latest("987", planted) is None
    assert store.find(ArtifactKey("987", "12345", "1", planted)) is None
    assert evil.file_id not in [r.file_id for r in store.prune("987", dry_run=True)]


# ----- final review: racing overwrites and a bare final-chunk response -------------------------


def _live_for_key(fake: FakeDrive) -> list[str]:
    """Live artifact files for the key (987, 12345, 1, wheel), oldest first."""
    return [
        f.id
        for f in sorted(fake.live_files(), key=lambda f: f.created_time)
        if f.app_properties.get("da.kind") == "artifact"
        and f.app_properties.get("da.name") == "wheel"
    ]


def test_racing_overwrites_of_one_key_keep_the_newer_upload_live(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two `--overwrite` puts of one key. B runs from start to finish after A's upload has
    landed and before A lists what to trash: A must not trash B's newer upload."""
    path, digest, size = _zip(tmp_path, "w.zip", b"original")
    original = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    path_a, digest_a, size_a = _zip(tmp_path, "a.zip", b"from A")
    path_b, digest_b, size_b = _zip(tmp_path, "b.zip", b"from B!")
    real = drive.upload_file
    raced: list[ArtifactRecord] = []
    started = [False]

    def upload(*args: Any, **kwargs: Any) -> DriveFile:
        uploaded = real(*args, **kwargs)
        if not started[0]:  # A's upload has landed; B now runs whole before A goes on
            started[0] = True
            raced.append(
                store.put(
                    ctx,
                    "wheel",
                    path_b,
                    sha256=digest_b,
                    size=size_b,
                    retention_days=1,
                    overwrite=True,
                )
            )
        return uploaded

    monkeypatch.setattr(drive, "upload_file", upload)
    a = store.put(
        ctx, "wheel", path_a, sha256=digest_a, size=size_a, retention_days=1, overwrite=True
    )
    (b,) = raced
    assert fake.files[a.file_id].created_time < fake.files[b.file_id].created_time
    assert not fake.files[b.file_id].trashed  # the older put never trashes the newer upload
    assert fake.files[a.file_id].trashed  # the newer put trashed the older one: last one wins
    assert fake.files[original.file_id].trashed
    assert _live_for_key(fake) == [b.file_id]  # never zero artifacts for the key
    assert _found(store.find(original.key)).file_id == b.file_id


def test_an_overwrite_never_trashes_a_record_newer_than_its_own_upload(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another job's upload lands between ours and our cleanup (its own cleanup still to come)."""
    path, digest, size = _zip(tmp_path, "w.zip", b"original")
    original = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    parent = fake.files[original.file_id].parents[0]
    real = drive.upload_file
    newer: list[str] = []

    def upload(*args: Any, **kwargs: Any) -> DriveFile:
        uploaded = real(*args, **kwargs)
        newer.append(_add_duplicate(fake, parent, digest))
        return uploaded

    monkeypatch.setattr(drive, "upload_file", upload)
    path2, digest2, size2 = _zip(tmp_path, "w2.zip", b"two!")
    mine = store.put(
        ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True
    )
    (theirs,) = newer
    assert fake.files[original.file_id].trashed
    assert _live_for_key(fake) == [mine.file_id, theirs]  # theirs trashes ours when it finishes
    assert _found(store.find(mine.key)).file_id == theirs


def _metadata_gets(fake: FakeDrive, file_id: str) -> int:
    return sum(
        1
        for r in fake.requests
        if r.method == "GET"
        and r.url.path.endswith(f"/files/{file_id}")
        and r.url.params.get("alt") != "media"
    )


def test_a_bare_final_chunk_response_is_completed_from_drive_before_the_size_check(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    """Drive returns size, createdTime and webViewLink on the last chunk only when the session's
    `fields` asked for them; when they are missing, `put` reads the file instead of guessing."""
    fake.bare_final_response = True
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    first = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    stored = fake.files[first.file_id]
    assert not stored.trashed  # the good upload was not mistaken for a 0-byte one
    assert first.size == size
    assert first.web_view_link == f"https://drive.google.com/file/d/{first.file_id}/view"
    assert first.created_time == stored.created_time
    assert _metadata_gets(fake, first.file_id) == 1
    path2, digest2, size2 = _zip(tmp_path, "w2.zip", b"two!")
    second = store.put(
        ctx, "wheel", path2, sha256=digest2, size=size2, retention_days=1, overwrite=True
    )
    assert second.size == size2
    assert fake.files[first.file_id].trashed
    assert _live_for_key(fake) == [second.file_id]


def test_a_full_final_chunk_response_needs_no_extra_read(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    rec = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    assert _metadata_gets(fake, rec.file_id) == 0


def _list_requests(fake: FakeDrive) -> int:
    return sum(1 for r in fake.requests if r.method == "GET" and r.url.path.endswith("/files"))


def test_latest_reads_the_first_page_only(
    store: ArtifactStore, ctx: RunContext, fake: FakeDrive, tmp_path: Path
) -> None:
    """The newest artifact is on the first page of a newest-first listing: a repository with
    thousands of artifacts must not be listed in full for one download."""
    path, digest, size = _zip(tmp_path, "w.zip", b"one")
    first = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    parent = fake.files[first.file_id].parents[0]
    for _ in range(250):
        _add_duplicate(fake, parent, digest)
    before = _list_requests(fake)
    assert store.latest("987", "wheel") is not None
    assert _list_requests(fake) - before == 1
    before = _list_requests(fake)
    assert len(store.list_repo("987")) == 251  # a listing still walks every page
    assert _list_requests(fake) - before == 3


def test_a_failed_re_read_after_a_bare_final_response_trashes_the_upload(
    store: ArtifactStore,
    drive: DriveClient,
    ctx: RunContext,
    fake: FakeDrive,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upload is in Drive but its size could not be verified: like a size mismatch, it must
    not stay live, or a plain retry would conflict with it."""
    fake.bare_final_response = True
    real = drive.get_file
    fail_next = [True]

    def get_file(file_id: str) -> DriveFile:
        if fail_next[0]:
            fail_next[0] = False
            raise DriveError("Drive answered 500", status=500, reason="backendError")
        return real(file_id)

    monkeypatch.setattr(drive, "get_file", get_file)
    path, digest, size = _zip(tmp_path, "w.zip", b"payload")
    with pytest.raises(DriveError, match="500"):
        store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)
    artifacts = [f for f in fake.files.values() if f.app_properties.get("da.kind") == "artifact"]
    assert [f.trashed for f in artifacts] == [True]
    retry = store.put(ctx, "wheel", path, sha256=digest, size=size, retention_days=1)  # no conflict
    assert not fake.files[retry.file_id].trashed
