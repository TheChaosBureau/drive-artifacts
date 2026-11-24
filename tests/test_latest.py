# SPDX-License-Identifier: Apache-2.0
"""The "latest" mode: one plain file at a fixed path, replaced in place under one link."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from drive_artifacts.archive import sha256_file
from drive_artifacts.drive import DriveClient
from drive_artifacts.errors import DriveError, NotFoundError, UsageError
from drive_artifacts.github import default_branch
from drive_artifacts.model import (
    FOLDER_MIME,
    LatestRecord,
    Repo,
    RunContext,
    latest_location,
    latest_properties,
)
from drive_artifacts.oauth import RefreshingTokenSource
from drive_artifacts.settings import Settings
from drive_artifacts.store import ArtifactStore
from tests.fake_drive import FakeDrive, Fault
from tests.test_cli import Run

API = "https://www.googleapis.com"
WHEN = datetime(2026, 11, 23, 9, 30, 0, tzinfo=UTC)
FOLDERS = ("Latest", "TheChaosBureau", "drive-artifacts")


def _client(fake: FakeDrive) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(fake.handler), headers={"Authorization": "Bearer at-1"}
    )


def _file(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


@pytest.fixture
def latest_store(drive: DriveClient) -> ArtifactStore:
    return ArtifactStore(drive, root_name="GitHub Artifacts", now=lambda: WHEN)


def _path_of(fake: FakeDrive, file_id: str) -> list[str]:
    """The names from the top of the Drive down to the file, read off the parents."""
    names: list[str] = []
    current: str | None = file_id
    while current is not None:
        f = fake.files[current]
        names.append(f.name)
        current = f.parents[0] if f.parents else None
    return names[::-1]


# ----- the fake ---------------------------------------------------------------
def test_the_fake_replaces_content_in_place_and_merges_properties() -> None:
    fake = FakeDrive()
    c = _client(fake)
    original = fake.files[
        fake.add_file(
            "paper.pdf", b"one", props={"da.kind": "latest", "da.commit": "old", "x": "y"}
        )
    ]
    created = original.created_time
    r = c.patch(
        f"{API}/upload/drive/v3/files/{original.id}",
        params={"uploadType": "resumable", "fields": "id,size,appProperties,mimeType"},
        json={"mimeType": "application/pdf", "appProperties": {"da.commit": "new", "x": None}},
        headers={"X-Upload-Content-Length": "5"},
    )
    assert r.status_code == 200
    r = c.put(r.headers["location"], content=b"three", headers={"Content-Range": "bytes 0-4/5"})
    assert r.status_code == 200
    assert r.json() == {
        "id": original.id,
        "size": "5",
        "mimeType": "application/pdf",
        "appProperties": {"da.kind": "latest", "da.commit": "new"},  # merged; the null deleted x
    }
    stored = fake.files[original.id]
    assert stored.content == b"three"
    assert stored.created_time == created
    assert stored.modified_time > created
    assert len(fake.files) == 1


def test_the_fake_refuses_an_update_session_it_cannot_serve() -> None:
    fake = FakeDrive()
    c = _client(fake)
    params = {"uploadType": "resumable"}
    r = c.patch(f"{API}/upload/drive/v3/files/nope", params=params, json={})
    assert r.status_code == 404
    folder = fake.add_folder("Latest")
    r = c.patch(f"{API}/upload/drive/v3/files/{folder}", params=params, json={})
    assert r.status_code == 403
    f = fake.add_file("a", b"")
    r = c.patch(f"{API}/upload/drive/v3/files/{f}", params=params, json={"parents": ["x"]})
    assert r.status_code == 400
    assert "does not model changing parents" in r.text
    # A file that vanished between the opening PATCH and the last chunk.
    r = c.patch(f"{API}/upload/drive/v3/files/{f}", params=params)
    del fake.files[f]
    r = c.put(r.headers["location"], content=b"x", headers={"Content-Range": "bytes 0-0/1"})
    assert r.status_code == 404


# ----- the client -------------------------------------------------------------
def test_update_file_keeps_the_id_and_sends_the_content_in_chunks(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    first = drive.upload_file(
        _file(tmp_path, "a.pdf", b"a" * 10),
        name="a.pdf",
        parent=None,
        app_properties={"da.kind": "latest", "da.commit": "one"},
        mime="application/pdf",
    )
    payload = bytes(range(256)) * 10  # 2560 bytes: three 1024-byte chunks
    second = drive.update_file(
        first.id,
        _file(tmp_path, "b.pdf", payload),
        app_properties={"da.commit": "two"},
        mime="application/pdf",
    )
    assert second.id == first.id
    assert second.web_view_link == first.web_view_link
    assert second.created_time == first.created_time
    assert second.name == "a.pdf"
    assert second.size == 2560
    assert second.app_properties == {"da.kind": "latest", "da.commit": "two"}
    assert fake.files[first.id].content == payload
    assert len(fake.live_files()) == 1
    opened = next(r for r in fake.requests if r.method == "PATCH")
    assert opened.url.path == f"/upload/drive/v3/files/{first.id}"
    assert opened.headers["X-Upload-Content-Length"] == "2560"
    assert json.loads(opened.content) == {
        "mimeType": "application/pdf",
        "appProperties": {"da.commit": "two"},
    }
    puts = [r for r in fake.requests if r.method == "PUT"][1:]
    assert [r.headers["Content-Range"] for r in puts] == [
        "bytes 0-1023/2560",
        "bytes 1024-2047/2560",
        "bytes 2048-2559/2560",
    ]


def test_update_file_of_a_missing_file_is_not_found(drive: DriveClient, tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        drive.update_file("gone", _file(tmp_path, "a", b"x"), app_properties={}, mime="text/plain")


def test_update_file_survives_a_dropped_chunk_and_an_expired_session(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    f = fake.add_file("a.txt", b"old", props={"da.kind": "latest"})
    src = _file(tmp_path, "a.txt", b"n" * 3000)
    fake.faults.append(Fault(method="PUT", path="upload_id", drop=True))
    assert drive.update_file(f, src, app_properties={}, mime="text/plain").size == 3000
    fake.faults.append(Fault(status=404, method="PUT", path="upload_id", reason="notFound"))
    src.write_bytes(b"m" * 2000)
    assert drive.update_file(f, src, app_properties={}, mime="text/plain").size == 2000
    assert fake.files[f].content == b"m" * 2000
    assert len(fake.live_files()) == 1


# ----- the model --------------------------------------------------------------
def test_the_default_location_is_latest_owner_repo_file() -> None:
    assert latest_location(None, "TheChaosBureau/drive-artifacts", "paper.pdf") == (
        FOLDERS,
        "paper.pdf",
    )
    assert latest_location("  ", "o/r", "a.txt") == (("Latest", "o", "r"), "a.txt")


def test_a_given_location_names_the_file_or_with_a_trailing_slash_its_folder() -> None:
    assert latest_location("Papers/index.html", "o/r", "out.html") == (("Papers",), "index.html")
    assert latest_location("Papers/2026/", "o/r", "out.html") == (("Papers", "2026"), "out.html")
    assert latest_location("index.html", "o/r", "out.html") == ((), "index.html")


@pytest.mark.parametrize(
    "spec", ["/abs.pdf", "a//b.pdf", "a/../b.pdf", "./b.pdf", "a/ b.pdf", "a\\b.pdf", "a/b\n.pdf"]
)
def test_a_location_with_a_step_that_is_no_name_is_a_usage_error(spec: str) -> None:
    with pytest.raises(UsageError, match="latest path"):
        latest_location(spec, "o/r", "x.pdf")


def test_a_file_name_that_is_not_utf8_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="not valid UTF-8"):
        latest_location(None, "o/r", "bad\udcff.pdf")


def test_latest_properties_carry_the_source_and_respect_the_property_limit(
    ctx: RunContext,
) -> None:
    props = latest_properties(
        ctx, folder="Latest/o/r", sha256="f" * 64, uploaded="2026-11-23T09:30:00Z"
    )
    assert props == {
        "da.v": "1",
        "da.kind": "latest",
        "da.dir": "Latest/o/r",
        "da.repo": "987",
        "da.slug": "TheChaosBureau/drive-artifacts",
        "da.run": "12345",
        "da.attempt": "1",
        "da.ref": "refs/heads/main",
        "da.commit": "a" * 40,
        "da.sha256": "f" * 64,
        "da.uploaded": "2026-11-23T09:30:00Z",
    }
    with pytest.raises(UsageError, match=r"'da\.dir' exceeds"):
        latest_properties(ctx, folder="d" * 119, sha256="f" * 64, uploaded="t")


def test_a_latest_record_round_trips_and_refuses_what_is_not_one() -> None:
    props = {
        "da.v": "1",
        "da.kind": "latest",
        "da.dir": "",
        "da.repo": "987",
        "da.slug": "o/r",
        "da.run": "5",
        "da.attempt": "2",
        "da.sha256": "f" * 64,
        "da.uploaded": "2026-11-23T09:30:00Z",
    }
    file = {"id": "id-1", "name": "index.html", "size": "12", "appProperties": props}
    record = LatestRecord.from_drive(file)
    assert record.path == "index.html"  # straight under the root
    assert (record.ref, record.commit, record.size) == ("", "", 12)
    assert record.to_json()["run_attempt"] == "2"
    with pytest.raises(ValueError, match="not a drive-artifacts latest file"):
        LatestRecord.from_drive({"id": "x", "appProperties": {**props, "da.kind": "artifact"}})
    with pytest.raises(ValueError, match="not a drive-artifacts latest file"):
        LatestRecord.from_drive({"id": "x", "appProperties": {**props, "da.v": "2"}})
    with pytest.raises(ValueError, match="not a drive-artifacts latest file"):
        LatestRecord.from_drive({"id": "x"})
    del props["da.uploaded"]
    with pytest.raises(ValueError, match="malformed drive-artifacts latest file 'id-1'"):
        LatestRecord.from_drive(file)
    with pytest.raises(ValueError, match="malformed"):
        LatestRecord.from_drive({**file, "size": "many"})


# ----- the store --------------------------------------------------------------
def test_put_latest_creates_one_plain_file_at_the_path(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    src = _file(tmp_path, "paper.pdf", b"%PDF-1.7 first")
    record = latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    stored = fake.files[record.file_id]
    assert stored.content == b"%PDF-1.7 first"  # the file itself, not a zip of it
    assert stored.mime_type == "application/pdf"
    assert _path_of(fake, record.file_id) == ["GitHub Artifacts", *FOLDERS, "paper.pdf"]
    assert "da.expires" not in stored.app_properties
    assert record == LatestRecord(
        file_id=stored.id,
        name="paper.pdf",
        folder="Latest/TheChaosBureau/drive-artifacts",
        repo_id="987",
        repo="TheChaosBureau/drive-artifacts",
        run_id="12345",
        run_attempt="1",
        ref="refs/heads/main",
        commit="a" * 40,
        sha256=sha256_file(src),
        uploaded="2026-11-23T09:30:00Z",
        size=14,
        web_view_link=f"https://drive.google.com/file/d/{stored.id}/view",
        created_time=stored.created_time,
        modified_time=stored.modified_time,
    )
    assert record.path == "Latest/TheChaosBureau/drive-artifacts/paper.pdf"
    folders = [f for f in fake.live_files() if f.mime_type == FOLDER_MIME]
    assert sorted(f.app_properties["da.kind"] for f in folders) == ["dir", "dir", "dir", "root"]


def test_a_second_put_latest_replaces_the_content_of_the_same_file(
    drive: DriveClient, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    times = iter([WHEN, WHEN.replace(hour=17)])
    store = ArtifactStore(drive, root_name="GitHub Artifacts", now=lambda: next(times))
    first = store.put_latest(
        ctx, _file(tmp_path, "one.pdf", b"first"), folders=FOLDERS, name="paper.pdf"
    )
    later = RunContext(repo=ctx.repo, run_id="12399", run_attempt="2", ref=ctx.ref, commit="b" * 40)
    second = store.put_latest(
        later, _file(tmp_path, "two.pdf", b"second, longer"), folders=FOLDERS, name="paper.pdf"
    )
    assert second.file_id == first.file_id
    assert second.web_view_link == first.web_view_link
    assert second.created_time == first.created_time
    assert (second.commit, second.run_id, second.run_attempt) == ("b" * 40, "12399", "2")
    assert (first.uploaded, second.uploaded) == ("2026-11-23T09:30:00Z", "2026-11-23T17:30:00Z")
    assert second.size == 14
    assert fake.files[first.file_id].content == b"second, longer"
    files = [f for f in fake.live_files() if f.mime_type != FOLDER_MIME]
    assert [f.id for f in files] == [first.file_id]
    assert len([f for f in fake.files.values() if f.mime_type == FOLDER_MIME]) == 4  # no new folder
    assert store.list_latest() == [second]


def test_put_latest_takes_a_mime_type_and_falls_back_for_an_unknown_ending(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    src = _file(tmp_path, "blob", b"\x00\x01")
    plain = latest_store.put_latest(ctx, src, folders=(), name="blob.zzunknown")
    assert fake.files[plain.file_id].mime_type == "application/octet-stream"
    assert _path_of(fake, plain.file_id) == ["GitHub Artifacts", "blob.zzunknown"]
    assert plain.folder == ""
    given = latest_store.put_latest(ctx, src, folders=(), name="page", mime="text/html")
    assert fake.files[given.file_id].mime_type == "text/html"


def test_two_paths_with_the_same_file_name_are_two_files(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    src = _file(tmp_path, "main.pdf", b"x")
    a = latest_store.put_latest(ctx, src, folders=("Latest", "o", "a"), name="main.pdf")
    b = latest_store.put_latest(ctx, src, folders=("Latest", "o", "b"), name="main.pdf")
    assert a.file_id != b.file_id
    assert [r.path for r in latest_store.list_latest()] == [
        "Latest/o/a/main.pdf",
        "Latest/o/b/main.pdf",
    ]
    # `Latest` and `o` are shared; only the last step differs.
    assert len([f for f in fake.live_files() if f.mime_type == FOLDER_MIME]) == 5


def test_a_name_with_a_quote_is_found_again(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    src = _file(tmp_path, "a", b"x")
    first = latest_store.put_latest(ctx, src, folders=("It's",), name="o'clock.txt")
    second = latest_store.put_latest(ctx, src, folders=("It's",), name="o'clock.txt")
    assert second.file_id == first.file_id
    assert len(fake.live_files()) == 3  # root, folder, file


def test_put_latest_leaves_artifacts_and_strangers_alone(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    """A file of the same name in the folder that is not a "latest" file is never replaced, and
    the artifact listings never see a latest file."""
    src = _file(tmp_path, "paper.pdf", b"mine")
    first = latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    folder = fake.files[first.file_id].parents[0]
    stranger = fake.add_file("paper.pdf", b"theirs", parent=folder, props={"da.kind": "artifact"})
    again = latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    assert again.file_id == first.file_id
    assert fake.files[stranger].content == b"theirs"
    assert not fake.files[stranger].trashed
    assert latest_store.list_repo(ctx.repo.id) == []
    assert latest_store.prune(ctx.repo.id) == []


def test_duplicates_at_a_path_collapse_onto_the_oldest_file(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    """What a lost race leaves: two files at one path. The older keeps its link and takes the
    new content; the newer goes to the trash."""
    src = _file(tmp_path, "paper.pdf", b"v1")
    first = latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    folder = fake.files[first.file_id].parents[0]
    twin = fake.add_file(
        "paper.pdf", b"twin", parent=folder, props=dict(fake.files[first.file_id].app_properties)
    )
    src.write_bytes(b"v2!")
    record = latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    assert record.file_id == first.file_id
    assert fake.files[first.file_id].content == b"v2!"
    assert fake.files[twin].trashed
    assert [r.file_id for r in latest_store.list_latest()] == [first.file_id]


def test_a_first_upload_that_lost_a_race_moves_onto_the_older_file(
    tokens: RefreshingTokenSource,
    settings: Settings,
    fake: FakeDrive,
    ctx: RunContext,
    tmp_path: Path,
) -> None:
    """Another run creates the file between our look and our upload, with a creation time
    before ours: its file is the keeper, ours is trashed, and the keeper ends with our content."""
    src = _file(tmp_path, "paper.pdf", b"ours")
    rival: list[str] = []
    inner = fake.handler

    def racing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "uploadType" in str(request.url) and not rival:
            meta = json.loads(request.content)
            rival.append(
                fake.add_file(
                    "paper.pdf", b"rival", parent=meta["parents"][0], props=meta["appProperties"]
                )
            )
        return inner(request)

    racing_drive = DriveClient(
        httpx.Client(transport=httpx.MockTransport(racing)), tokens, settings, chunk_size=1024
    )
    store = ArtifactStore(racing_drive, root_name="GitHub Artifacts", now=lambda: WHEN)
    record = store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    assert record.file_id == rival[0]
    assert fake.files[rival[0]].content == b"ours"
    live = [f for f in fake.live_files() if f.mime_type != FOLDER_MIME]
    assert [f.id for f in live] == rival
    assert len([f for f in fake.files.values() if f.mime_type != FOLDER_MIME]) == 2


def test_put_latest_reads_the_file_again_when_the_last_chunk_answers_bare(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    fake.bare_final_response = True
    src = _file(tmp_path, "paper.pdf", b"content")
    record = latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    assert (record.size, record.commit) == (7, "a" * 40)
    assert record.web_view_link


def test_a_size_drive_records_differently_fails_and_keeps_the_canonical_file(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    src = _file(tmp_path, "paper.pdf", b"content")
    first = latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    inner = fake._replace

    def lossy(session, content):  # type: ignore[no-untyped-def]
        return inner(session, content[:-1])

    fake._replace = lossy  # type: ignore[method-assign]
    with pytest.raises(DriveError, match="recorded 6 bytes for a 7-byte upload") as exc:
        latest_store.put_latest(ctx, src, folders=FOLDERS, name="paper.pdf")
    assert exc.value.reason == "sizeMismatch"
    assert not fake.files[first.file_id].trashed


def test_list_latest_filters_by_repository_and_skips_what_it_cannot_read(
    latest_store: ArtifactStore, fake: FakeDrive, ctx: RunContext, tmp_path: Path
) -> None:
    src = _file(tmp_path, "main.pdf", b"x")
    mine = latest_store.put_latest(ctx, src, folders=("Latest", "o", "a"), name="main.pdf")
    other_ctx = RunContext(
        repo=Repo(id="555", slug="o/b"), run_id="1", run_attempt="1", ref="r", commit="c"
    )
    other = latest_store.put_latest(other_ctx, src, folders=("Latest", "o", "b"), name="main.pdf")
    fake.add_file("future.pdf", b"", props={"da.kind": "latest", "da.v": "2"})
    fake.add_file("broken.pdf", b"", props={"da.kind": "latest", "da.v": "1", "da.repo": "987"})
    gone = latest_store.put_latest(ctx, src, folders=("Old",), name="gone.pdf")
    fake.files[gone.file_id].trashed = True
    assert latest_store.list_latest() == [mine, other]
    assert latest_store.list_latest(repo_id="555") == [other]
    assert latest_store.list_latest(repo_id="1") == []


# ----- the default branch -----------------------------------------------------
def test_default_branch_comes_from_the_event_payload(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"default_branch": "trunk"}}))
    assert default_branch({"GITHUB_EVENT_PATH": str(event)}) == "trunk"


@pytest.mark.parametrize(
    "payload", ["not json", "[]", "{}", '{"repository": 1}', '{"repository": {}}',
                '{"repository": {"default_branch": ""}}', '{"repository": {"default_branch": 3}}']
)  # fmt: skip
def test_default_branch_is_unknown_without_a_usable_payload(tmp_path: Path, payload: str) -> None:
    event = tmp_path / "event.json"
    event.write_text(payload)
    assert default_branch({"GITHUB_EVENT_PATH": str(event)}) is None
    assert default_branch({"GITHUB_EVENT_PATH": str(tmp_path / "missing.json")}) is None
    assert default_branch({}) is None


# ----- the command line -------------------------------------------------------
@pytest.fixture
def run(fake: FakeDrive, tmp_path: Path) -> Run:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"default_branch": "main"}}))
    r = Run(fake, tmp_path, GITHUB_EVENT_PATH=str(event))
    (r.cwd / "paper").mkdir()
    (r.cwd / "paper" / "main.pdf").write_bytes(b"%PDF first")
    (r.cwd / "paper" / "main.log").write_bytes(b"log")
    return r


def _again(run: Run, **env: str) -> Run:
    """A later run in the same checkout against the same Drive."""
    later = Run(run.fake, run.cwd.parent / f"later-{len(env)}-{len(run.fake.requests)}")
    later.cwd = run.cwd
    later.env = {**run.env, "GITHUB_OUTPUT": str(later.out_file), **env}
    later.env["GITHUB_STEP_SUMMARY"] = str(later.summary_file)
    return later


def test_upload_latest_twice_leaves_one_file_with_the_same_link_and_the_second_content(
    run: Run, fake: FakeDrive
) -> None:
    assert run("upload", "--latest", "--path", "paper/main.pdf", "--json") == 0, run.stderr
    first = run.outputs()
    assert fake.files[first["artifact-id"]].content == b"%PDF first"
    assert first["size"] == "10"
    assert first["artifact-digest"].startswith("sha256:")
    assert "Latest/TheChaosBureau/drive-artifacts/main.pdf" in run.summary_file.read_text()
    assert "latest Latest/TheChaosBureau/drive-artifacts/main.pdf" in run.stderr.getvalue()
    assert run.json()["path"] == "Latest/TheChaosBureau/drive-artifacts/main.pdf"
    (run.cwd / "paper" / "main.pdf").write_bytes(b"%PDF second, longer")
    later = _again(run, GITHUB_RUN_ID="12346", GITHUB_SHA="b" * 40)
    assert later("upload", "--latest", "--path", "paper/main.pdf") == 0, later.stderr
    second = later.outputs()
    assert second["artifact-id"] == first["artifact-id"]
    assert second["artifact-url"] == first["artifact-url"]
    assert second["artifact-digest"] != first["artifact-digest"]
    assert fake.files[first["artifact-id"]].content == b"%PDF second, longer"
    assert [f.name for f in fake.live_files() if f.mime_type != FOLDER_MIME] == ["main.pdf"]
    listing = _again(run)
    assert listing("latest", "--json") == 0
    (entry,) = listing.json()
    assert entry["path"] == "Latest/TheChaosBureau/drive-artifacts/main.pdf"
    assert entry["url"] == first["artifact-url"]
    assert entry["file_id"] == first["artifact-id"]
    assert (entry["repo"], entry["repo_id"]) == ("TheChaosBureau/drive-artifacts", "987")
    assert (entry["commit"], entry["ref"]) == ("b" * 40, "refs/heads/main")
    assert (entry["run_id"], entry["run_attempt"]) == ("12346", "1")
    assert entry["sha256"] == second["artifact-digest"].removeprefix("sha256:")
    assert entry["size"] == 19
    assert datetime.fromisoformat(entry["uploaded"]).tzinfo is not None


def test_the_latest_listing_needs_only_a_token_and_prints_one_line_per_file(
    run: Run, fake: FakeDrive, tmp_path: Path
) -> None:
    assert run("upload", "--latest", "--path", "paper/main.pdf") == 0
    bare = Run(fake, tmp_path / "elsewhere")
    for name in list(bare.env):
        if name.startswith("GITHUB_"):
            del bare.env[name]
    assert bare("--verbose", "latest") == 0, bare.stderr.getvalue()
    (line,) = bare.stdout.getvalue().splitlines()
    path, repo, commit, uploaded, url = line.split("\t")
    assert path == "Latest/TheChaosBureau/drive-artifacts/main.pdf"
    assert (repo, commit) == ("TheChaosBureau/drive-artifacts", "a" * 40)
    assert uploaded.endswith("Z")
    assert url.startswith("https://drive.google.com/file/d/")
    assert "listing the latest files" in bare.stderr.getvalue()
    only = Run(fake, tmp_path / "filtered")
    assert only("--verbose", "latest", "--repo-id", "1", "--json") == 0
    assert only.json() == []
    assert "of repository 1" in only.stderr.getvalue()


def test_upload_latest_takes_a_path_of_its_own(run: Run, fake: FakeDrive) -> None:
    assert run("upload", "--latest", "--path", "paper/main.pdf", "--latest-path", "Papers/") == 0
    assert run("upload", "--latest", "--path", "paper/main.pdf", "--latest-path", "x/y.pdf") == 0
    files = {f.name: f for f in fake.live_files() if f.mime_type != FOLDER_MIME}
    assert _path_of(fake, files["main.pdf"].id) == ["GitHub Artifacts", "Papers", "main.pdf"]
    assert _path_of(fake, files["y.pdf"].id) == ["GitHub Artifacts", "x", "y.pdf"]
    bad = _again(run)
    assert bad("upload", "--latest", "--path", "paper/main.pdf", "--latest-path", "../up") == 2
    assert "latest path" in bad.stderr.getvalue()


@pytest.mark.parametrize(
    ("ref", "event"),
    [
        ("refs/pull/7/merge", '{"repository": {"default_branch": "main"}}'),
        ("refs/heads/feature", '{"repository": {"default_branch": "main"}}'),
        ("refs/tags/v1.0.0", '{"repository": {"default_branch": "main"}}'),
        ("refs/heads/main", '{"repository": {"default_branch": "trunk"}}'),
        ("", '{"repository": {"default_branch": "main"}}'),
    ],
)
def test_upload_latest_is_refused_off_the_default_branch(
    run: Run, fake: FakeDrive, ref: str, event: str
) -> None:
    Path(run.env["GITHUB_EVENT_PATH"]).write_text(event)
    run.env["GITHUB_REF"] = ref
    assert run("upload", "--latest", "--path", "paper/main.pdf") == 2
    assert "--latest runs only on the default branch (refs/heads/" in run.stderr.getvalue()
    assert "Pass --any-ref to allow it" in run.stderr.getvalue()
    assert fake.requests == []  # refused before Drive is asked anything
    allowed = _again(run)
    assert allowed("upload", "--latest", "--any-ref", "--path", "paper/main.pdf") == 0


def test_upload_latest_needs_to_know_the_default_branch(run: Run, fake: FakeDrive) -> None:
    del run.env["GITHUB_EVENT_PATH"]
    assert run("upload", "--latest", "--path", "paper/main.pdf") == 2
    assert "does not say which that is" in run.stderr.getvalue()
    assert fake.requests == []
    named = _again(run)
    assert named("upload", "--latest", "--default-branch", "main", "--path", "paper/main.pdf") == 0
    other = _again(run)
    assert other("upload", "--latest", "--default-branch", "trunk", "--path", "paper/main.pdf") == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["--name", "paper"],
        ["--retention-days", "7"],
        ["--compression-level", "0"],
        ["--overwrite"],
        ["--prune"],
        ["--no-prune"],
    ],
)
def test_upload_latest_refuses_the_options_of_the_zip_mode(
    run: Run, fake: FakeDrive, extra: list[str]
) -> None:
    assert run("upload", "--latest", "--path", "paper/main.pdf", *extra) == 2
    assert f"{extra[0].replace('--no-', '--')} cannot be combined with --latest" in (
        run.stderr.getvalue()
    )
    assert fake.requests == []


@pytest.mark.parametrize(
    "extra", [["--latest-path", "Papers/"], ["--default-branch", "main"], ["--any-ref"]]
)
def test_the_options_of_the_latest_mode_need_it(
    run: Run, fake: FakeDrive, extra: list[str]
) -> None:
    assert run("upload", "--path", "paper/main.pdf", *extra) == 2
    assert f"{extra[0]} needs --latest" in run.stderr.getvalue()
    assert fake.requests == []


def test_upload_latest_stores_exactly_one_file(run: Run, fake: FakeDrive) -> None:
    assert run("upload", "--latest", "--path", "paper/") == 2
    assert "--latest stores one file, and --path matched 2" in run.stderr.getvalue()
    assert fake.requests == []
    one = _again(run)
    assert one("upload", "--latest", "--path", "paper/\n!paper/*.log") == 0
    assert [f.name for f in fake.live_files() if f.mime_type != FOLDER_MIME] == ["main.pdf"]


@pytest.mark.parametrize(
    ("mode", "code", "said"), [("warn", 0, "warning"), ("error", 7, "error"), ("ignore", 0, "")]
)
def test_upload_latest_with_nothing_to_upload_follows_if_no_files_found(
    run: Run, fake: FakeDrive, mode: str, code: int, said: str
) -> None:
    (run.cwd / "empty").mkdir()
    for path in ("paper/none.pdf", "empty/"):
        attempt = _again(run)
        assert attempt("upload", "--latest", "--path", path, "--if-no-files-found", mode) == code
        text = attempt.stderr.getvalue()
        assert (f"::{said}::no files matched" in text) if said else (text == "")
    assert fake.requests == []


def test_the_zip_upload_still_defaults_as_before(run: Run, fake: FakeDrive) -> None:
    """The zip options moved their defaults out of the parser; the behaviour did not move."""
    assert run("upload", "--path", "paper/", "--json") == 0
    body = run.json()
    assert body["name"] == "artifact"
    assert body["expires"] > "2026"
    (stored,) = [f for f in fake.live_files() if f.mime_type != FOLDER_MIME]
    assert stored.name == "12345-1-artifact.zip"
    assert stored.mime_type == "application/zip"
    listing = _again(run)
    assert listing("latest", "--json") == 0  # and an artifact is not a latest file
    assert listing.json() == []
