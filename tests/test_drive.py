# SPDX-License-Identifier: Apache-2.0
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from drive_artifacts.drive import DriveClient, DriveFile, RetryPolicy
from drive_artifacts.errors import DriveError, NotFoundError
from drive_artifacts.model import FOLDER_MIME
from drive_artifacts.oauth import RefreshingTokenSource, StoredToken
from drive_artifacts.settings import Settings
from tests.fake_drive import CutStream, FakeDrive, Fault


def test_drive_file_round_trip() -> None:
    data = {
        "id": "i",
        "name": "n",
        "mimeType": FOLDER_MIME,
        "parents": ["p"],
        "appProperties": {"a": "1"},
        "createdTime": "c",
        "modifiedTime": "m",
        "webViewLink": "w",
        "trashed": False,
    }
    f = DriveFile.from_json(data)
    assert f.size is None
    assert f.parents == ("p",)
    assert DriveFile.from_json({**data, "size": "7"}).size == 7
    assert f.as_mapping()["appProperties"] == {"a": "1"}
    assert (f.as_mapping()["createdTime"], f.as_mapping()["modifiedTime"]) == ("c", "m")


def test_create_folder_list_get_trash(drive: DriveClient, fake: FakeDrive) -> None:
    root = drive.create_folder("R", parent=None, app_properties={"da.kind": "root"})
    child = drive.create_folder(
        "C", parent=root.id, app_properties={"da.kind": "repo", "da.repo": "1"}
    )
    assert child.parents == (root.id,)
    assert child.mime_type == FOLDER_MIME
    query = "appProperties has { key='da.repo' and value='1' } and trashed = false"
    found = drive.list_files(query)
    assert [f.id for f in found] == [child.id]
    assert drive.get_file(child.id).name == "C"
    drive.trash(child.id)
    assert drive.list_files(query) == []
    assert fake.files[child.id].trashed
    with pytest.raises(NotFoundError):
        drive.get_file("missing")


def test_list_walks_every_page(drive: DriveClient, fake: FakeDrive) -> None:
    for i in range(7):
        fake.add_file(f"f{i}", b"", props={"k": "v"})
    query = "appProperties has { key='k' and value='v' } and trashed = false"
    assert len(drive.list_files(query, page_size=3)) == 7
    listings = [
        r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/drive/v3/files")
    ]
    assert len(listings) == 3  # 3 + 3 + 1: every page was asked for


def test_401_refreshes_the_token_once(drive: DriveClient, fake: FakeDrive) -> None:
    drive.list_files("trashed = false")
    fake.expire_access_tokens()
    assert drive.list_files("trashed = false") == []
    assert sum(1 for r in fake.requests if r.url.path.endswith("/token")) == 2


def test_rate_limits_back_off_with_full_jitter(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float]
) -> None:
    fake.faults.append(Fault(status=403, reason="userRateLimitExceeded", method="GET", count=2))
    fake.faults.append(Fault(status=429, reason="rateLimitExceeded", method="GET", count=1))
    assert drive.list_files("trashed = false") == []
    assert sleeps == [1.0, 2.0, 4.0]


def test_gives_up_after_max_attempts(drive: DriveClient, fake: FakeDrive) -> None:
    fake.faults.append(Fault(status=503, method="GET", count=99))
    with pytest.raises(DriveError, match="5 attempts") as info:
        drive.list_files("trashed = false")
    assert info.value.status == 503


def test_non_retryable_403_fails_fast(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float]
) -> None:
    fake.faults.append(Fault(status=403, reason="insufficientPermissions", method="GET"))
    with pytest.raises(DriveError, match="insufficientPermissions"):
        drive.list_files("trashed = false")
    assert sleeps == []


def test_dropped_connections_are_retried(drive: DriveClient, fake: FakeDrive) -> None:
    fake.faults.append(Fault(drop=True, method="GET", count=2))
    assert drive.list_files("trashed = false") == []


def test_deadline_stops_retries(
    http: httpx.Client, tokens: RefreshingTokenSource, settings: Settings, fake: FakeDrive
) -> None:
    now = [0.0]
    drive = DriveClient(
        http,
        tokens,
        settings,
        policy=RetryPolicy(max_attempts=50, deadline_s=10.0),
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
        rng=lambda: 1.0,
    )
    fake.faults.append(Fault(status=503, method="GET", count=99))
    with pytest.raises(DriveError, match="deadline"):
        drive.list_files("trashed = false")


def test_a_second_401_fails_instead_of_looping(drive: DriveClient, fake: FakeDrive) -> None:
    fake.faults.append(Fault(status=401, reason="authError", method="GET", count=99))
    with pytest.raises(DriveError, match="freshly refreshed") as info:
        drive.list_files("trashed = false")
    assert info.value.status == 401
    # one token to start with, one forced refresh after the first 401, and no more
    assert sum(1 for r in fake.requests if r.url.path.endswith("/token")) == 2


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<html>Bad Request</html>", ""),
        (b"[1, 2]", ""),
        (b'{"error": "nope"}', ""),
        (b'{"error": {"status": "INVALID_ARGUMENT"}}', "INVALID_ARGUMENT"),
        (b'{"error": {"errors": [{"reason": "invalidQuery"}]}}', "invalidQuery"),
    ],
)
def test_error_reason_is_read_from_whatever_drive_sends(
    tokens: RefreshingTokenSource, settings: Settings, sleeps: list[float], body: bytes, reason: str
) -> None:
    odd = _drive_over(lambda _r: httpx.Response(400, content=body), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="HTTP 400") as info:
        odd.list_files("trashed = false")
    assert info.value.status == 400
    assert info.value.reason == reason
    assert sleeps == []


def test_chunk_size_must_be_a_multiple_of_256_kib(
    http: httpx.Client, tokens: RefreshingTokenSource, settings: Settings
) -> None:
    with pytest.raises(ValueError, match="256 KiB"):
        DriveClient(http, tokens, settings, chunk_size=300 * 1024)
    DriveClient(http, tokens, settings, chunk_size=512 * 1024)
    DriveClient(http, tokens, settings)  # the 8 MiB default


def test_list_files_passes_the_order_through(drive: DriveClient, fake: FakeDrive) -> None:
    older = fake.add_file("a", b"", props={"k": "v"})
    newer = fake.add_file("b", b"", props={"k": "v"})
    found = drive.list_files(
        "appProperties has { key='k' and value='v' }", order_by="modifiedTime desc"
    )
    assert [f.id for f in found] == [newer, older]


def test_the_bearer_token_travels_only_in_the_authorization_header(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(1500))
    uploaded = drive.upload_file(src, name="a.zip", parent=None, app_properties={"k": "v"})
    drive.list_files("trashed = false")
    drive.download_file(uploaded.id, tmp_path / "b.zip", expected_size=1500)
    drive.trash(uploaded.id)
    secret = next(iter(fake.access_tokens))
    drive_calls = [r for r in fake.requests if not r.url.path.endswith("/token")]
    assert drive_calls
    for request in drive_calls:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        assert secret not in str(request.url)
        assert secret.encode() not in request.content


def _blob(n: int) -> bytes:
    return bytes(i % 251 for i in range(n))


def _content_ranges(fake: FakeDrive) -> list[str]:
    """The Content-Range of every PUT the fake saw, in order (chunks and offset queries)."""
    return [r.headers["Content-Range"] for r in fake.requests if r.method == "PUT"]


def _drive_over(
    handler: Callable[[httpx.Request], httpx.Response],
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
) -> DriveClient:
    """A client whose Drive calls go through `handler` (tokens still come from the real fake)."""
    return DriveClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        tokens,
        settings,
        policy=RetryPolicy(max_attempts=5, deadline_s=60.0),
        sleep=sleeps.append,
        rng=lambda: 1.0,
        chunk_size=1024,
    )


def _chunks_fail(fake: FakeDrive) -> Callable[[httpx.Request], httpx.Response]:
    """Every chunk PUT gets a 503; `bytes */total` offset queries reach the fake."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and not request.headers["Content-Range"].startswith("bytes */"):
            return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return fake.handler(request)

    return handler


def test_upload_in_chunks_sets_metadata(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2500))
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={"da.kind": "artifact"})
    assert fake.files[f.id].content == _blob(2500)
    assert f.app_properties == {"da.kind": "artifact"}
    assert f.size == 2500
    puts = [r for r in fake.requests if r.method == "PUT"]
    assert [r.headers["Content-Range"] for r in puts] == [
        "bytes 0-1023/2500",
        "bytes 1024-2047/2500",
        "bytes 2048-2499/2500",
    ]
    start = next(r for r in fake.requests if r.method == "POST" and "uploadType" in str(r.url))
    assert start.headers["X-Upload-Content-Length"] == "2500"
    assert start.headers["X-Upload-Content-Type"] == "application/zip"


def test_upload_resumes_from_the_servers_offset(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(3000))
    # the first chunk is only partly received, then a later chunk gets a 5xx
    fake.faults.append(Fault(method="PUT", path="upload_id", truncate_to=100))
    fake.faults.append(Fault(status=503, method="PUT", path="upload_id"))
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(3000)
    # The recorded requests prove the resume paths ran: the 308 offset (100) was honoured, the 5xx
    # was followed by a `bytes */total` offset query, and the retry went from the server's offset.
    assert _content_ranges(fake) == [
        "bytes 0-1023/3000",  # the server keeps 100 bytes of it and answers 308
        "bytes 100-1123/3000",  # resumed from that offset; this one meets the 503
        "bytes */3000",  # after a failed chunk the client asks the session where it stands
        "bytes 100-1123/3000",  # and resumes from the answer
        "bytes 1124-2147/3000",
        "bytes 2148-2999/3000",
    ]


def test_upload_survives_a_dropped_chunk(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2048))
    fake.faults.append(Fault(drop=True, method="PUT", path="upload_id"))
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(2048)
    assert _content_ranges(fake) == [
        "bytes 0-1023/2048",  # dropped before it was sent
        "bytes */2048",  # nothing received: the 308 carries no Range, so offset 0
        "bytes 0-1023/2048",
        "bytes 1024-2047/2048",
    ]


def test_upload_resends_a_chunk_the_server_kept_none_of(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2048))
    fake.faults.append(Fault(method="PUT", path="upload_id", truncate_to=0))
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(2048)
    assert _content_ranges(fake) == [
        "bytes 0-1023/2048",
        "bytes 0-1023/2048",  # a 308 without a Range header means nothing was kept
        "bytes 1024-2047/2048",
    ]


def test_upload_of_an_empty_file(drive: DriveClient, fake: FakeDrive, tmp_path: Path) -> None:
    src = tmp_path / "empty.zip"
    src.write_bytes(b"")
    f = drive.upload_file(src, name="empty.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == b""
    assert f.size == 0
    assert _content_ranges(fake) == ["bytes */0"]


def test_upload_places_the_file_under_its_parent(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    folder = drive.create_folder("R", parent=None, app_properties={})
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(10))
    f = drive.upload_file(src, name="a.zip", parent=folder.id, app_properties={}, mime="text/plain")
    assert f.parents == (folder.id,)
    start = next(r for r in fake.requests if r.method == "POST" and "uploadType" in str(r.url))
    assert start.headers["X-Upload-Content-Type"] == "text/plain"


def test_upload_finds_a_file_whose_last_response_was_lost(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2000))

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)  # the server processes the final chunk...
        if request.headers.get("Content-Range") == "bytes 1024-1999/2000":
            return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return response  # ...but its answer never reaches the client

    lossy = _drive_over(handler, tokens, settings, sleeps)
    f = lossy.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(2000)
    assert len(fake.live_files()) == 1  # found complete, not uploaded a second time
    assert _content_ranges(fake) == ["bytes 0-1023/2000", "bytes 1024-1999/2000", "bytes */2000"]
    assert sleeps == []  # the session is asked before any backoff, and it was already complete


def test_upload_gives_up_when_chunks_keep_failing(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2000))
    stubborn = _drive_over(_chunks_fail(fake), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="upload gave up after 5 attempts") as info:
        stubborn.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert info.value.status == 503
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert fake.live_files() == []


def test_upload_deadline_stops_retries(
    fake: FakeDrive, tokens: RefreshingTokenSource, settings: Settings, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2000))
    now = [0.0]
    patient = DriveClient(
        httpx.Client(transport=httpx.MockTransport(_chunks_fail(fake))),
        tokens,
        settings,
        policy=RetryPolicy(max_attempts=50, deadline_s=10.0),
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
        rng=lambda: 1.0,
        chunk_size=1024,
    )
    with pytest.raises(DriveError, match="upload gave up at the 10 s deadline"):
        patient.upload_file(src, name="a.zip", parent=None, app_properties={})


def test_errors_never_echo_the_upload_session_url(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(10))
    fake.faults.append(Fault(status=400, method="PUT", path="upload_id"))
    with pytest.raises(DriveError, match="HTTP 400") as info:
        drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert "upload_id" not in str(info.value)
    assert "upload-" not in str(info.value)


def _chunk_put(request: httpx.Request) -> bool:
    """A PUT that carries file bytes, as opposed to a `bytes */total` offset query."""
    return request.method == "PUT" and not request.headers["Content-Range"].startswith("bytes */")


def _timed_client(
    handler: Callable[[httpx.Request], httpx.Response],
    tokens: RefreshingTokenSource,
    settings: Settings,
    now: list[float],
    *,
    deadline_s: float,
) -> DriveClient:
    """A client on a fake clock that only moves when the handler or a backoff sleep moves it."""
    return DriveClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        tokens,
        settings,
        policy=RetryPolicy(max_attempts=50, deadline_s=deadline_s),
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
        rng=lambda: 1.0,
        chunk_size=1024,
    )


def test_a_healthy_upload_may_outlive_the_deadline(
    fake: FakeDrive, tokens: RefreshingTokenSource, settings: Settings, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(4000))
    now = [0.0]
    hiccups = [1]

    def handler(request: httpx.Request) -> httpx.Response:
        if _chunk_put(request):
            now[0] += 5.0  # every chunk takes five seconds
            if request.headers["Content-Range"] == "bytes 3072-3999/4000" and hiccups[0]:
                hiccups[0] -= 1  # the last chunk meets one 503, 20 s into a 10 s deadline
                return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return fake.handler(request)

    patient = _timed_client(handler, tokens, settings, now, deadline_s=10.0)
    f = patient.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(4000)
    assert now[0] > 20.0  # well past the deadline in total, but never 10 s without progress
    assert hiccups == [0]


def test_the_upload_deadline_counts_from_the_last_progress(
    fake: FakeDrive, tokens: RefreshingTokenSource, settings: Settings, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(4000))
    now = [0.0]
    stalled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _chunk_put(request):
            now[0] += 5.0
            if request.headers["Content-Range"].startswith("bytes 2048-"):
                stalled.append(
                    request.headers["Content-Range"]
                )  # from here on, nothing gets through
                return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return fake.handler(request)

    stuck = _timed_client(handler, tokens, settings, now, deadline_s=10.0)
    with pytest.raises(DriveError, match="upload gave up at the 10 s deadline"):
        stuck.upload_file(src, name="a.zip", parent=None, app_properties={})
    # Two chunks went through (t=5 and t=10). The first stalled try at t=15 is 5 s after the last
    # progress, still inside the deadline; the second at t=21 is 11 s after it. Measured from the
    # start of the upload the first would already have been over the line.
    assert stalled == ["bytes 2048-3071/4000"] * 2


def test_an_upload_stuck_at_the_same_offset_is_a_failure_not_success(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float], tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2048))
    fake.faults.append(Fault(method="PUT", path="upload_id", truncate_to=0, count=99))
    with pytest.raises(DriveError, match="upload is not making progress") as info:
        drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert info.value.status == 308
    assert sleeps == [1.0, 2.0, 4.0, 8.0]  # backoff between the five attempts
    assert _content_ranges(fake) == ["bytes 0-1023/2048"] * 5
    assert fake.live_files() == []


def test_progress_between_stalls_resets_the_upload_attempts(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float], tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2048))
    # Four stalls (one short of max_attempts), 50 bytes of progress, four more stalls: eight in
    # all, which only completes if the progress in the middle starts the count again.
    fake.faults.append(Fault(method="PUT", path="upload_id", truncate_to=0, count=4))
    fake.faults.append(Fault(method="PUT", path="upload_id", truncate_to=50))
    fake.faults.append(Fault(method="PUT", path="upload_id", truncate_to=0, count=4))
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(2048)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 1.0, 2.0, 4.0, 8.0]


def _opening_with(
    fake: FakeDrive, seen: list[httpx.Request], location: Callable[[str], str | None]
) -> Callable[[httpx.Request], httpx.Response]:
    """The fake, except that the answer to the opening POST carries `location(real_location)`."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response = fake.handler(request)
        if request.method == "POST" and "uploadType" in str(request.url):
            where = location(response.headers["Location"])
            return httpx.Response(200, headers={} if where is None else {"Location": where})
        return response

    return handler


def test_an_opening_response_without_a_location_is_an_error(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(10))
    seen: list[httpx.Request] = []
    drive = _drive_over(_opening_with(fake, seen, lambda _real: None), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="Drive opened no upload session"):
        drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert not any(r.method == "PUT" for r in seen)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/upload/drive/v3/files?uploadType=resumable&upload_id=x",
        "http://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=x",
        "https://www.googleapis.com:8443/upload/drive/v3/files?uploadType=resumable&upload_id=x",
    ],
)
def test_a_session_url_on_another_origin_is_never_sent_the_token(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
    location: str,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(10))
    seen: list[httpx.Request] = []
    drive = _drive_over(_opening_with(fake, seen, lambda _real: location), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="upload session URL points at an unexpected host") as info:
        drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert "upload_id" not in str(info.value)  # the host is named, the credential is not
    # the only request was the opening POST: nothing was ever sent to the suspect URL
    assert [r.method for r in seen] == ["POST"]


def test_a_relative_session_url_resolves_against_the_upload_host(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(10))
    seen: list[httpx.Request] = []
    relative = _opening_with(fake, seen, lambda real: httpx.URL(real).raw_path.decode())
    f = _drive_over(relative, tokens, settings, sleeps).upload_file(
        src, name="a.zip", parent=None, app_properties={}
    )
    assert fake.files[f.id].content == _blob(10)
    assert {r.url.host for r in seen} == {"www.googleapis.com"}


def test_expired_session_restarts_once(drive: DriveClient, fake: FakeDrive, tmp_path: Path) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(1500))
    fake.faults.append(Fault(status=404, reason="notFound", method="PUT", path="upload_id"))
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(1500)
    fake.faults.append(
        Fault(status=404, reason="notFound", method="PUT", path="upload_id", count=99)
    )
    with pytest.raises(DriveError, match="session"):
        drive.upload_file(src, name="b.zip", parent=None, app_properties={})


def test_download_is_atomic_and_resumes(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(5000))
    dest = tmp_path / "out" / "a.zip"
    # the first GET fails before any byte (a plain retry), the second fails mid-stream (Range resume)
    fake.faults.append(Fault(drop=True, method="GET", path=fid))
    fake.faults.append(Fault(method="GET", path=fid, cut_after=2000))
    assert drive.download_file(fid, dest, expected_size=5000) == 5000
    assert dest.read_bytes() == _blob(5000)
    assert not dest.with_name("a.zip.part").exists()
    ranged = [
        r.headers["Range"] for r in fake.requests if r.method == "GET" and "Range" in r.headers
    ]
    assert ranged == ["bytes=2000-"]
    # dropped GET, cut GET, resumed GET: three media requests, and only the last carried a Range
    gets = [r for r in fake.requests if r.method == "GET" and r.url.params.get("alt") == "media"]
    assert [r.headers.get("Range") for r in gets] == [None, None, "bytes=2000-"]


def test_download_size_mismatch_is_an_error(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(10))
    with pytest.raises(DriveError, match="size") as info:
        drive.download_file(fid, tmp_path / "a.zip", expected_size=11)
    assert info.value.status == 416  # Drive had nothing past byte 10 to give
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "a.zip.part").exists()


def test_download_longer_than_expected_is_an_error(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(10))
    with pytest.raises(DriveError, match="got 10 bytes, expected 5"):
        drive.download_file(fid, tmp_path / "a.zip", expected_size=5)
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "a.zip.part").exists()


def test_download_without_an_expected_size_takes_what_drive_sends(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(10))
    assert drive.download_file(fid, tmp_path / "a.zip") == 10
    assert (tmp_path / "a.zip").read_bytes() == _blob(10)


def test_download_starts_over_a_stale_part_file(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(10))
    (tmp_path / "a.zip.part").write_bytes(b"junk from an earlier run")
    assert drive.download_file(fid, tmp_path / "a.zip", expected_size=10) == 10
    assert (tmp_path / "a.zip").read_bytes() == _blob(10)
    assert not any("Range" in r.headers for r in fake.requests)


def test_download_missing_is_not_found(drive: DriveClient, tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        drive.download_file("nope", tmp_path / "x")
    assert not (tmp_path / "x.part").exists()


def test_download_gives_up_when_streams_keep_breaking(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float], tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(5000))
    # every stream dies before its first byte: no progress, so the attempts add up
    fake.faults.append(Fault(method="GET", path=fid, cut_after=0, count=99))
    with pytest.raises(DriveError, match="download gave up after 5 attempts"):
        drive.download_file(fid, tmp_path / "a.zip", expected_size=5000)
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "a.zip.part").exists()


def test_download_breaks_that_each_bring_bytes_do_not_exhaust_the_attempts(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float], tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(5000))
    # six cuts, more than max_attempts (5), but each one comes after 100 new bytes
    fake.faults.append(Fault(method="GET", path=fid, cut_after=100, count=6))
    assert drive.download_file(fid, tmp_path / "a.zip", expected_size=5000) == 5000
    assert (tmp_path / "a.zip").read_bytes() == _blob(5000)
    assert sleeps == [1.0] * 6  # the backoff starts over after every pass that made progress
    ranged = [r.headers["Range"] for r in fake.requests if "Range" in r.headers]
    assert ranged == [f"bytes={n}-" for n in range(100, 700, 100)]


def test_a_download_whose_error_body_is_cut_is_retried(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float], tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(5000))
    # a 503 whose JSON body breaks after 10 bytes: read outside the stream's own guard
    fake.faults.append(Fault(status=503, method="GET", path=fid, cut_after=10))
    assert drive.download_file(fid, tmp_path / "a.zip", expected_size=5000) == 5000
    assert (tmp_path / "a.zip").read_bytes() == _blob(5000)
    assert sleeps == [1.0]
    assert fake.faults == []


def test_error_bodies_that_keep_breaking_end_as_a_drive_error(
    drive: DriveClient, fake: FakeDrive, sleeps: list[float], tmp_path: Path
) -> None:
    fid = fake.add_file("a.zip", _blob(5000))
    fake.faults.append(Fault(status=503, method="GET", path=fid, cut_after=10, count=99))
    with pytest.raises(DriveError, match="gave up after 5 attempts") as info:
        drive.download_file(fid, tmp_path / "a.zip", expected_size=5000)
    assert info.value.status == 503
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "a.zip.part").exists()


def _trickle(data: bytes, *, honour_range: bool) -> Callable[[httpx.Request], httpx.Response]:
    """A server that sends one clean byte per request (or the same head again if it ignores Range)."""

    def handler(request: httpx.Request) -> httpx.Response:
        spec = request.headers.get("Range")
        start = int(spec.removeprefix("bytes=").removesuffix("-")) if spec else 0
        if not honour_range:
            return httpx.Response(200, content=data[:5])
        if start:
            return httpx.Response(
                206,
                content=data[start : start + 1],
                headers={"Content-Range": f"bytes {start}-{start}/{len(data)}"},
            )
        return httpx.Response(200, content=data[:1])

    return handler


def test_download_of_short_clean_streams_completes_while_it_moves_forward(
    tokens: RefreshingTokenSource, settings: Settings, sleeps: list[float], tmp_path: Path
) -> None:
    # 100 clean responses of one byte each (far more than max_attempts): every pass adds a byte,
    # so none of them counts against the attempts and nothing is slept.
    slow = _drive_over(_trickle(_blob(100), honour_range=True), tokens, settings, sleeps)
    assert slow.download_file("f", tmp_path / "a.zip", expected_size=100) == 100
    assert (tmp_path / "a.zip").read_bytes() == _blob(100)
    assert sleeps == []


def test_download_of_an_empty_file(drive: DriveClient, fake: FakeDrive, tmp_path: Path) -> None:
    fid = fake.add_file("empty.zip", b"")
    assert drive.download_file(fid, tmp_path / "empty.zip", expected_size=0) == 0
    assert (tmp_path / "empty.zip").read_bytes() == b""
    assert not (tmp_path / "empty.zip.part").exists()


def test_download_that_makes_no_progress_is_a_size_mismatch(
    tokens: RefreshingTokenSource, settings: Settings, sleeps: list[float], tmp_path: Path
) -> None:
    # The server ignores Range and sends the same five bytes again: nothing new arrived.
    deaf = _drive_over(_trickle(_blob(100), honour_range=False), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="size mismatch: got 5 bytes, expected 10"):
        deaf.download_file("f", tmp_path / "a.zip", expected_size=10)
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "a.zip.part").exists()


def test_download_cut_after_the_last_byte_is_complete(
    tokens: RefreshingTokenSource, settings: Settings, sleeps: list[float], tmp_path: Path
) -> None:
    data = _blob(10)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(data))},
            stream=CutStream(data, len(data), request),  # every byte, then a ReadError
        )

    drive = _drive_over(handler, tokens, settings, sleeps)
    assert drive.download_file("f", tmp_path / "a.zip", expected_size=10) == 10
    assert (tmp_path / "a.zip").read_bytes() == data
    assert len(requests) == 1  # nothing left to ask for, so no Range request
    assert sleeps == []  # and nothing to wait for: the file is complete


_fault = st.one_of(
    st.just(Fault(status=503, method="PUT", path="upload_id")),
    st.just(Fault(status=429, reason="rateLimitExceeded", method="PUT", path="upload_id")),
    st.just(Fault(drop=True, method="PUT", path="upload_id")),
    st.integers(min_value=0, max_value=1023).map(
        lambda n: Fault(method="PUT", path="upload_id", truncate_to=n)
    ),
)


@h_settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(size=st.integers(min_value=1, max_value=5000), faults=st.lists(_fault, max_size=6))
def test_upload_converges_under_random_faults(
    tmp_path: Path, size: int, faults: list[Fault]
) -> None:
    fake = FakeDrive()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    settings = Settings.from_env(
        {"DRIVE_ARTIFACTS_CLIENT_ID": "cid", "DRIVE_ARTIFACTS_CLIENT_SECRET": "csec"}
    )
    tokens = RefreshingTokenSource(
        http, settings, StoredToken("rt-1", "cid"), sleep=lambda _s: None
    )
    drive = DriveClient(
        http,
        tokens,
        settings,
        policy=RetryPolicy(max_attempts=20, deadline_s=600),
        sleep=lambda _s: None,
        rng=lambda: 0.0,
        chunk_size=1024,
    )
    fake.faults.extend(Fault(**vars(f)) for f in faults)
    src = tmp_path / f"{size}.zip"
    src.write_bytes(_blob(size))
    f = drive.upload_file(src, name="z", parent=None, app_properties={"k": "v"})
    assert fake.files[f.id].content == _blob(size)


# ----- edges from the pre-release reviews ------------------------------------------------------


def _stored_but_503(
    fake: FakeDrive, *, plain_failures: int
) -> Callable[[httpx.Request], httpx.Response]:
    """The first `plain_failures` chunk PUTs fail outright. Every later one reaches the server,
    which stores it, but the answer is lost and the client sees a 503."""
    seen = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if _chunk_put(request):
            seen[0] += 1
            if seen[0] > plain_failures:
                fake.handler(request)
            return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return fake.handler(request)

    return handler


def test_progress_made_by_the_last_attempt_in_the_budget_is_credited(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2000))
    # max_attempts is 5: four plain failures, then the fifth PUT is stored although it "failed"
    drive = _drive_over(_stored_but_503(fake, plain_failures=4), tokens, settings, sleeps)
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(2000)
    assert sleeps == [1.0, 2.0, 4.0, 8.0]  # four real stalls; the stored chunks cost no backoff


def test_a_backoff_never_sleeps_past_the_deadline(
    fake: FakeDrive, tokens: RefreshingTokenSource, settings: Settings
) -> None:
    now = [0.0]
    fake.faults.append(Fault(status=503, method="GET", count=99))
    patient = _timed_client(fake.handler, tokens, settings, now, deadline_s=10.0)
    with pytest.raises(DriveError, match="gave up at the 10 s deadline"):
        patient.get_file("x")
    assert now[0] == 10.0  # 1 + 2 + 4, then 3 of the next 8: not 15


def test_an_upload_backoff_never_sleeps_past_the_deadline(
    fake: FakeDrive, tokens: RefreshingTokenSource, settings: Settings, tmp_path: Path
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2000))
    now = [0.0]
    patient = _timed_client(_chunks_fail(fake), tokens, settings, now, deadline_s=10.0)
    with pytest.raises(DriveError, match="upload gave up at the 10 s deadline"):
        patient.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert now[0] == 10.0


def test_a_failing_offset_query_shares_the_upload_budget(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    """A stall is one budget of `max_attempts`, not one per offset query inside it."""
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2000))
    puts = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":  # chunks and offset queries alike
            puts[0] += 1
            return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return fake.handler(request)

    drive = _drive_over(handler, tokens, settings, sleeps)
    with pytest.raises(DriveError, match="upload gave up after 5 attempts"):
        drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert puts[0] <= 6  # the chunk, then at most one offset query per attempt
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_a_download_stops_at_the_deadline(
    tokens: RefreshingTokenSource, settings: Settings, tmp_path: Path
) -> None:
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": "10"}, stream=CutStream(_blob(10), 0, request)
        )

    patient = _timed_client(handler, tokens, settings, now, deadline_s=10.0)
    with pytest.raises(DriveError, match="download gave up at the 10 s deadline"):
        patient.download_file("f", tmp_path / "a.zip", expected_size=10)
    assert now[0] == 10.0
    assert not (tmp_path / "a.zip").exists()


def _answering(response: Callable[[httpx.Request], httpx.Response | None], fake: FakeDrive):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return response(request) or fake.handler(request)

    return handler


def test_a_success_that_is_not_json_is_a_drive_error(
    fake: FakeDrive, tokens: RefreshingTokenSource, settings: Settings, sleeps: list[float]
) -> None:
    """A captive portal or proxy answering 200 with its own page."""
    portal = lambda _request: httpx.Response(200, text="<html>Sign in to the network</html>")  # noqa: E731
    drive = _drive_over(_answering(portal, fake), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="not the JSON"):
        drive.get_file("x")
    with pytest.raises(DriveError, match="not the JSON"):
        drive.list_files("trashed = false")
    with pytest.raises(DriveError, match="not the JSON"):
        drive.create_folder("f", parent=None, app_properties={})


@pytest.mark.parametrize("body", [["not", "a", "file"], {"kind": "drive#file"}])
def test_json_that_is_not_a_file_is_a_drive_error(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    body: object,
) -> None:
    drive = _drive_over(
        _answering(lambda _r: httpx.Response(200, json=body), fake), tokens, settings, sleeps
    )
    with pytest.raises(DriveError, match="not the JSON"):
        drive.get_file("x")


def test_a_redirect_is_never_written_as_file_content(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    redirect = lambda _r: httpx.Response(  # noqa: E731
        302, headers={"Location": "https://example.test/login"}, text="<html>moved</html>"
    )
    drive = _drive_over(_answering(redirect, fake), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="HTTP 302"):
        drive.download_file("f", tmp_path / "a.zip")
    assert not (tmp_path / "a.zip").exists()
    assert list(tmp_path.iterdir()) == []
    assert sleeps == []


@pytest.mark.parametrize("header", ["garbage", "bytes=0-", "bytes=0-x", "bytes=5"])
def test_a_malformed_range_on_a_308_is_a_drive_error(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
    header: str,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(2000))
    bad = lambda r: httpx.Response(308, headers={"Range": header}) if _chunk_put(r) else None  # noqa: E731
    drive = _drive_over(_answering(bad, fake), tokens, settings, sleeps)
    with pytest.raises(DriveError, match="malformed Range"):
        drive.upload_file(src, name="a.zip", parent=None, app_properties={})


def test_a_malformed_session_location_is_a_drive_error(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(10))
    seen: list[httpx.Request] = []
    drive = _drive_over(
        _opening_with(fake, seen, lambda _real: "https://www.googleapis.com/up\x00load"),
        tokens,
        settings,
        sleeps,
    )
    with pytest.raises(DriveError, match="malformed upload session URL"):
        drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert not any(r.method == "PUT" for r in seen)


def _opening_posts(fake: FakeDrive) -> int:
    return sum(1 for r in fake.requests if r.method == "POST" and "uploadType" in str(r.url))


@pytest.mark.parametrize("status", [404, 410])
def test_an_expired_session_is_reopened_and_the_upload_starts_over(
    drive: DriveClient, fake: FakeDrive, tmp_path: Path, status: int
) -> None:
    """Drive documents "404 or 410" for a session that expired."""
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(1500))
    fake.faults.append(Fault(status=status, reason="notFound", method="PUT", path="upload_id"))
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(1500)
    assert _opening_posts(fake) == 2
    ranges = _content_ranges(fake)
    assert ranges[0] == "bytes 0-1023/1500"  # refused: the session was gone
    assert ranges[1:] == ["bytes 0-1023/1500", "bytes 1024-1499/1500"]  # from the start again


def test_an_access_token_that_expires_mid_upload_is_refreshed(
    fake: FakeDrive,
    tokens: RefreshingTokenSource,
    settings: Settings,
    sleeps: list[float],
    tmp_path: Path,
) -> None:
    """Access tokens last an hour; a long upload meets the expiry between two chunks."""
    src = tmp_path / "a.zip"
    src.write_bytes(_blob(3000))
    chunks = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if _chunk_put(request):
            chunks[0] += 1
            if chunks[0] == 2:
                fake.expire_access_tokens()
        return fake.handler(request)

    drive = _drive_over(handler, tokens, settings, sleeps)
    f = drive.upload_file(src, name="a.zip", parent=None, app_properties={})
    assert fake.files[f.id].content == _blob(3000)
    assert chunks[0] == 4  # three chunks, one of them sent twice: once stale, once fresh
    assert sleeps == []  # a refresh is not a failure: no backoff


@pytest.mark.parametrize("size", [0, -1024])
def test_chunk_size_must_be_positive(
    http: httpx.Client, tokens: RefreshingTokenSource, settings: Settings, size: int
) -> None:
    with pytest.raises(ValueError, match="positive"):
        DriveClient(http, tokens, settings, chunk_size=size)
