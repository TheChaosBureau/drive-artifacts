# SPDX-License-Identifier: Apache-2.0
import json
import time

import httpx
import pytest

from tests.fake_drive import FakeDrive, Fault
from tests.fake_server import serve

API = "https://www.googleapis.com"


def client(fake: FakeDrive, token: str = "at-1") -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(fake.handler), headers={"Authorization": f"Bearer {token}"}
    )


def test_refresh_grants_an_access_token_and_bad_refresh_is_invalid_grant() -> None:
    fake = FakeDrive()
    c = httpx.Client(transport=httpx.MockTransport(fake.handler))
    r = c.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "rt-1",
            "client_id": "cid",
            "client_secret": "csec",
        },
    )
    assert r.status_code == 200
    assert r.json()["access_token"] in fake.access_tokens
    r = c.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "nope",
            "client_id": "cid",
            "client_secret": "csec",
        },
    )
    assert (r.status_code, r.json()["error"]) == (400, "invalid_grant")


def test_unauthenticated_calls_are_401() -> None:
    fake = FakeDrive()
    r = client(fake, "bogus").get(f"{API}/drive/v3/files")
    assert r.status_code == 401


def test_create_list_query_and_pagination() -> None:
    fake = FakeDrive()
    c = client(fake)
    root = c.post(
        f"{API}/drive/v3/files",
        json={
            "name": "R",
            "mimeType": "application/vnd.google-apps.folder",
            "appProperties": {"da.kind": "root"},
        },
    ).json()
    for i in range(5):
        fake.add_file(
            f"f{i}", b"x", parent=root["id"], props={"da.kind": "artifact", "da.name": "n"}
        )
    fake.add_file("other", b"y", props={"da.kind": "artifact", "da.name": "m"})
    q = "appProperties has { key='da.name' and value='n' } and trashed = false"
    page1 = c.get(f"{API}/drive/v3/files", params={"q": q, "pageSize": 2}).json()
    assert len(page1["files"]) == 2
    assert "nextPageToken" in page1
    page2 = c.get(
        f"{API}/drive/v3/files", params={"q": q, "pageSize": 2, "pageToken": page1["nextPageToken"]}
    ).json()
    page3 = c.get(
        f"{API}/drive/v3/files", params={"q": q, "pageSize": 2, "pageToken": page2["nextPageToken"]}
    ).json()
    assert len(page2["files"]) == 2
    assert len(page3["files"]) == 1
    assert "nextPageToken" not in page3
    by_parent = c.get(
        f"{API}/drive/v3/files", params={"q": f"'{root['id']}' in parents and trashed = false"}
    ).json()
    assert len(by_parent["files"]) == 5
    folders = c.get(
        f"{API}/drive/v3/files",
        params={"q": "mimeType = 'application/vnd.google-apps.folder' and trashed = false"},
    ).json()
    assert [f["name"] for f in folders["files"]] == ["R"]
    assert c.get(f"{API}/drive/v3/files", params={"q": "garbage"}).status_code == 400


def test_escaped_values_in_queries() -> None:
    fake = FakeDrive()
    fake.add_file("a", b"", props={"da.name": "quinn's\\essay"})
    r = client(fake).get(
        f"{API}/drive/v3/files",
        params={
            "q": "appProperties has { key='da.name' and value='quinn\\'s\\\\essay' } and trashed = false"
        },
    )
    assert len(r.json()["files"]) == 1


def test_trash_hides_from_list_and_get_still_works() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"1", props={"da.kind": "artifact"})
    c = client(fake)
    assert c.patch(f"{API}/drive/v3/files/{fid}", json={"trashed": True}).status_code == 200
    assert c.get(f"{API}/drive/v3/files", params={"q": "trashed = false"}).json()["files"] == []
    assert c.get(f"{API}/drive/v3/files/{fid}").json()["trashed"] is True
    assert c.get(f"{API}/drive/v3/files/missing").status_code == 404


def test_order_by_created_time_ignores_metadata_patches_unlike_modified_time() -> None:
    fake = FakeDrive()
    first = fake.add_file("a", b"1")
    second = fake.add_file("b", b"2")
    third = fake.add_file("c", b"3")
    c = client(fake)
    # a metadata patch moves modifiedTime but never createdTime
    assert c.patch(f"{API}/drive/v3/files/{first}", json={"name": "a2"}).status_code == 200

    def listed(order_by: str | None) -> list[str]:
        params = {"q": "trashed = false"}
        if order_by is not None:
            params["orderBy"] = order_by
        return [f["id"] for f in c.get(f"{API}/drive/v3/files", params=params).json()["files"]]

    assert listed("createdTime desc") == [third, second, first]
    assert listed("createdTime") == [first, second, third]
    assert listed("modifiedTime desc") == [first, third, second]
    assert listed(None) == [first, second, third]


def test_media_download_with_range() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    c = client(fake)
    assert c.get(f"{API}/drive/v3/files/{fid}", params={"alt": "media"}).content == b"0123456789"
    r = c.get(f"{API}/drive/v3/files/{fid}", params={"alt": "media"}, headers={"Range": "bytes=4-"})
    assert (r.status_code, r.content, r.headers["content-range"]) == (
        206,
        b"456789",
        "bytes 4-9/10",
    )


def test_resumable_upload_308_semantics_and_truncation() -> None:
    fake = FakeDrive()
    c = client(fake)
    data = bytes(range(256)) * 4  # 1024 bytes
    r = c.post(
        f"{API}/upload/drive/v3/files",
        params={"uploadType": "resumable"},
        json={"name": "a.zip", "appProperties": {"da.kind": "artifact"}},
        headers={"X-Upload-Content-Length": "1024"},
    )
    assert r.status_code == 200
    session = r.headers["location"]
    # status query before any bytes: 308 without Range
    r = c.put(session, headers={"Content-Range": "bytes */1024"})
    assert r.status_code == 308
    assert "range" not in r.headers
    fake.faults.append(Fault(method="PUT", path="upload_id", truncate_to=100))
    r = c.put(session, content=data[:512], headers={"Content-Range": "bytes 0-511/1024"})
    assert (r.status_code, r.headers["range"]) == (308, "bytes=0-99")
    r = c.put(session, content=data[100:512], headers={"Content-Range": "bytes 100-511/1024"})
    assert (r.status_code, r.headers["range"]) == (308, "bytes=0-511")
    r = c.put(session, content=data[512:], headers={"Content-Range": "bytes 512-1023/1024"})
    assert r.status_code == 200
    file = r.json()
    assert fake.files[file["id"]].content == data
    assert file["size"] == "1024"
    assert file["appProperties"] == {"da.kind": "artifact"}
    assert c.put(session, headers={"Content-Range": "bytes */1024"}).status_code == 200


def test_expired_session_is_404() -> None:
    fake = FakeDrive()
    c = client(fake)
    session = c.post(
        f"{API}/upload/drive/v3/files", params={"uploadType": "resumable"}, json={"name": "a"}
    ).headers["location"]
    fake.expire_session(httpx.URL(session).params["upload_id"])
    assert c.put(session, headers={"Content-Range": "bytes */10"}).status_code == 404


def test_faults_fire_once_each_and_drop_raises() -> None:
    fake = FakeDrive()
    fake.faults.append(Fault(status=403, reason="userRateLimitExceeded", method="GET", count=2))
    fake.faults.append(Fault(drop=True, method="GET"))
    c = client(fake)
    for _ in range(2):
        r = c.get(f"{API}/drive/v3/files", params={"q": "trashed = false"})
        assert r.status_code == 403
        assert r.json()["error"]["errors"][0]["reason"] == "userRateLimitExceeded"
    try:
        c.get(f"{API}/drive/v3/files", params={"q": "trashed = false"})
    except httpx.ConnectError:
        pass
    else:
        raise AssertionError("drop fault should raise")
    assert c.get(f"{API}/drive/v3/files", params={"q": "trashed = false"}).status_code == 200
    assert fake.faults == []


def test_auth_code_exchange_checks_pkce() -> None:
    import base64
    import hashlib

    fake = FakeDrive()
    verifier = "v" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    c = httpx.Client(transport=httpx.MockTransport(fake.handler))
    # codes are single-use, like Google's: a failed exchange burns the code
    code = fake.issue_auth_code(challenge)
    bad = c.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "wrong",
            "client_id": "cid",
            "client_secret": "csec",
            "redirect_uri": "http://127.0.0.1:1/",
        },
    )
    assert bad.status_code == 400
    code = fake.issue_auth_code(challenge)
    good = c.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": "cid",
            "client_secret": "csec",
            "redirect_uri": "http://127.0.0.1:1/",
        },
    )
    body = good.json()
    assert body["refresh_token"] in fake.refresh_tokens
    assert body["access_token"] in fake.access_tokens
    assert (
        c.post(
            "https://oauth2.googleapis.com/revoke", params={"token": body["refresh_token"]}
        ).status_code
        == 200
    )
    assert body["refresh_token"] in fake.revoked
    assert body["refresh_token"] not in fake.refresh_tokens


def test_real_http_server_round_trip() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"payload")
    with serve(fake) as base:
        r = httpx.get(
            f"{base}/api/drive/v3/files/{fid}",
            params={"alt": "media"},
            headers={"Authorization": "Bearer at-1"},
        )
        assert r.content == b"payload"
        r = httpx.post(
            f"{base}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "rt-1",
                "client_id": "cid",
                "client_secret": "csec",
            },
        )
        assert r.status_code == 200
        session = httpx.post(
            f"{base}/api/upload/drive/v3/files",
            params={"uploadType": "resumable"},
            json={"name": "z"},
            headers={"Authorization": "Bearer at-1"},
        ).headers["location"]
        assert session.startswith(f"{base}/api/upload/")
        r = httpx.put(
            session,
            content=b"abc",
            headers={"Authorization": "Bearer at-1", "Content-Range": "bytes 0-2/3"},
        )
        assert r.status_code == 200
        assert json.loads(r.text)["size"] == "3"


def test_real_http_server_drop_fault_closes_the_connection_at_once() -> None:
    fake = FakeDrive()
    fake.faults.append(Fault(drop=True, method="GET"))
    headers = {"Authorization": "Bearer at-1"}
    with serve(fake) as base:
        # a disconnect (which class depends on the platform), not a ReadTimeout:
        # the client must not sit waiting for a response
        started = time.monotonic()
        with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
            httpx.get(
                f"{base}/api/drive/v3/files",
                params={"q": "trashed = false"},
                headers=headers,
                timeout=5,
            )
        assert time.monotonic() - started < 2
        r = httpx.get(
            f"{base}/api/drive/v3/files",
            params={"q": "trashed = false"},
            headers=headers,
            timeout=5,
        )
        assert r.status_code == 200


# ----- downloads: Range handling and cuts mid-stream --------------------------------


def drain(response: httpx.Response, into: bytearray) -> None:
    """Read a streamed body chunk by chunk, keeping what arrived even if the stream fails."""
    for chunk in response.iter_bytes():
        into += chunk


def media(c: httpx.Client, fid: str, rng: str | None = None) -> httpx.Response:
    headers = {"Range": rng} if rng else {}
    return c.get(f"{API}/drive/v3/files/{fid}", params={"alt": "media"}, headers=headers)


@pytest.mark.parametrize(
    ("rng", "status", "body", "content_range"),
    [
        (None, 200, b"0123456789", None),
        ("bytes=2-4", 206, b"234", "bytes 2-4/10"),
        ("bytes=4-", 206, b"456789", "bytes 4-9/10"),
        ("bytes=0-0", 206, b"0", "bytes 0-0/10"),
        ("bytes=8-100", 206, b"89", "bytes 8-9/10"),
        ("bytes=-3", 206, b"789", "bytes 7-9/10"),
        ("bytes=-100", 206, b"0123456789", "bytes 0-9/10"),
        ("bytes=10-", 416, b"", "bytes */10"),
        ("bytes=10-12", 416, b"", "bytes */10"),
        ("bytes=-0", 416, b"", "bytes */10"),
    ],
)
def test_media_range_forms(
    rng: str | None, status: int, body: bytes, content_range: str | None
) -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    r = media(client(fake), fid, rng)
    assert r.status_code == status
    assert r.headers.get("content-range") == content_range
    if status != 416:
        assert r.content == body
    else:
        assert r.json()["error"]["errors"][0]["reason"] == "requestedRangeNotSatisfiable"


def test_media_range_on_an_empty_file() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"")
    c = client(fake)
    assert (media(c, fid).status_code, media(c, fid).content) == (200, b"")
    for rng in ("bytes=0-", "bytes=-1"):
        r = media(c, fid, rng)
        assert (r.status_code, r.headers["content-range"]) == (416, "bytes */0")


@pytest.mark.parametrize("rng", ["bytes=5-2", "bytes=-", "items=0-1", "bytes=0-1,3-4", "bytes=a-b"])
def test_media_range_nonsense_is_400(rng: str) -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    assert media(client(fake), fid, rng).status_code == 400


def test_cut_after_delivers_the_head_of_the_body_then_fails_the_stream() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    fake.faults.append(Fault(method="GET", path="alt=media", cut_after=3))
    received = bytearray()
    with client(fake).stream("GET", f"{API}/drive/v3/files/{fid}", params={"alt": "media"}) as r:
        assert r.status_code == 200
        assert r.headers["content-length"] == "10"
        with pytest.raises(httpx.ReadError):
            drain(r, received)
    assert bytes(received) == b"012"
    assert fake.faults == []
    # the fault fired once; the next download is whole
    assert media(client(fake), fid).content == b"0123456789"


def test_cut_after_applies_to_the_ranged_body() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    fake.faults.append(Fault(method="GET", cut_after=3))
    received = bytearray()
    with client(fake).stream(
        "GET",
        f"{API}/drive/v3/files/{fid}",
        params={"alt": "media"},
        headers={"Range": "bytes=2-"},
    ) as r:
        assert (r.status_code, r.headers["content-range"]) == (206, "bytes 2-9/10")
        assert r.headers["content-length"] == "8"
        with pytest.raises(httpx.ReadError):
            drain(r, received)
    assert bytes(received) == b"234"


def test_real_http_server_cut_after_is_a_fast_transport_error() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    fake.faults.append(Fault(method="GET", path="alt=media", cut_after=3))
    received = bytearray()
    with serve(fake) as base:
        started = time.monotonic()
        with httpx.stream(
            "GET",
            f"{base}/api/drive/v3/files/{fid}",
            params={"alt": "media"},
            headers={"Authorization": "Bearer at-1"},
            timeout=5,
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-length"] == "10"
            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                drain(r, received)
        assert time.monotonic() - started < 2
        assert bytes(received) == b"012"
        r = httpx.get(
            f"{base}/api/drive/v3/files/{fid}",
            params={"alt": "media"},
            headers={"Authorization": "Bearer at-1", "Range": "bytes=-3"},
        )
        assert (r.status_code, r.content, r.headers["content-range"]) == (
            206,
            b"789",
            "bytes 7-9/10",
        )


def test_faults_with_no_meaning_for_the_request_answer_400() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    c = client(fake)
    # truncate_to is for resumable chunk PUTs
    fake.faults.append(Fault(method="GET", truncate_to=5))
    r = c.get(f"{API}/drive/v3/files", params={"q": "trashed = false"})
    assert r.status_code == 400
    assert "truncate_to" in r.json()["error"]["message"]
    assert fake.sessions == {}
    # cut_after is for alt=media downloads
    fake.faults.append(Fault(method="GET", cut_after=2))
    r = c.get(f"{API}/drive/v3/files", params={"q": "trashed = false"})
    assert r.status_code == 400
    assert "cut_after" in r.json()["error"]["message"]
    fake.faults.append(Fault(method="POST", cut_after=2))
    assert c.post(f"{API}/drive/v3/files", json={"name": "x"}).status_code == 400
    # ... and has to fall inside the body being sent
    for cut in (10, 99, -1):
        fake.faults.append(Fault(method="GET", cut_after=cut))
        r = media(c, fid)
        assert r.status_code == 400
        assert f"cut_after={cut}" in r.json()["error"]["message"]
    assert fake.faults == []
    assert media(c, fid).content == b"0123456789"


# ----- queries: the grammar between terms -------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "and trashed = false",
        "trashed = false appProperties has { key='k' and value='v' }",
        "trashed = false and and mimeType = 'x'",
        "trashed = false and",
        "trashed = false or mimeType = 'x'",
        "trashed = falseand mimeType = 'x'",
    ],
)
def test_query_joins_must_be_exactly_and(q: str) -> None:
    fake = FakeDrive()
    r = client(fake).get(f"{API}/drive/v3/files", params={"q": q})
    assert r.status_code == 400


def test_query_with_and_between_terms_is_accepted() -> None:
    fake = FakeDrive()
    fake.add_file("a", b"", mime="x")
    q = "trashed = false and mimeType = 'x' and name = 'a'"
    assert len(client(fake).get(f"{API}/drive/v3/files", params={"q": q}).json()["files"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"pageSize": "many"},
        {"pageSize": "0"},
        {"pageSize": "-2"},
        {"pageToken": "abc"},
        {"pageToken": "-1"},
        {"pageToken": "1.5"},
    ],
)
def test_bad_paging_parameters_are_400(params: dict[str, str]) -> None:
    fake = FakeDrive()
    r = client(fake).get(f"{API}/drive/v3/files", params={"q": "trashed = false", **params})
    assert r.status_code == 400


# ----- resumable uploads: the Content-Range has to agree with the body ----------------


def start_upload(c: httpx.Client, total: int) -> str:
    r = c.post(
        f"{API}/upload/drive/v3/files",
        params={"uploadType": "resumable"},
        json={"name": "a.zip"},
        headers={"X-Upload-Content-Length": str(total)},
    )
    return r.headers["location"]


def test_content_range_that_disagrees_with_the_body_is_400() -> None:
    fake = FakeDrive()
    c = client(fake)
    session = start_upload(c, 10)
    disagreeing = [
        (b"abcd", "bytes 0-4/10"),  # claims 5 bytes, carries 4
        (b"abcde", "bytes 0-3/10"),  # claims 4 bytes, carries 5
        (b"x" * 12, "bytes 0-11/10"),  # runs past the total
        (b"", "bytes 5-4/10"),  # reversed
        (b"abcd", "bytes 0-3/3"),  # the total is smaller than the chunk
    ]
    for body, header in disagreeing:
        r = c.put(session, content=body, headers={"Content-Range": header})
        assert r.status_code == 400, header
    # nothing was stored, and the session still starts at zero
    r = c.put(session, headers={"Content-Range": "bytes */10"})
    assert r.status_code == 308
    assert "range" not in r.headers
    assert fake.files == {}


def test_final_chunk_declaring_six_bytes_but_carrying_twelve_is_400() -> None:
    fake = FakeDrive()
    c = client(fake)
    session = start_upload(c, 10)
    r = c.put(session, content=b"abcd", headers={"Content-Range": "bytes 0-3/10"})
    assert (r.status_code, r.headers["range"]) == (308, "bytes=0-3")
    r = c.put(session, content=b"x" * 12, headers={"Content-Range": "bytes 4-9/10"})
    assert r.status_code == 400
    assert fake.files == {}
    r = c.put(session, headers={"Content-Range": "bytes */10"})
    assert (r.status_code, r.headers["range"]) == (308, "bytes=0-3")
    # the honest final chunk still completes the upload
    r = c.put(session, content=b"efghij", headers={"Content-Range": "bytes 4-9/10"})
    assert r.status_code == 200
    assert fake.files[r.json()["id"]].content == b"abcdefghij"


# ----- revoke ------------------------------------------------------------------------


def test_revoke_unknown_token_is_400_invalid_token() -> None:
    fake = FakeDrive()
    c = httpx.Client(transport=httpx.MockTransport(fake.handler))
    r = c.post("https://oauth2.googleapis.com/revoke", params={"token": "nope"})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_token")
    r = c.post("https://oauth2.googleapis.com/revoke", data={"token": "nope"})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_token")
    r = c.post("https://oauth2.googleapis.com/revoke")
    assert (r.status_code, r.json()["error"]) == (400, "invalid_token")
    assert fake.revoked == []
    assert fake.refresh_tokens == {"rt-1"}


def test_revoking_twice_succeeds_once_then_is_400() -> None:
    fake = FakeDrive()
    c = httpx.Client(transport=httpx.MockTransport(fake.handler))
    first = c.post("https://oauth2.googleapis.com/revoke", data={"token": "rt-1"})  # form body
    assert first.status_code == 200
    assert fake.refresh_tokens == set()
    assert fake.revoked == ["rt-1"]
    second = c.post("https://oauth2.googleapis.com/revoke", params={"token": "rt-1"})  # query
    assert (second.status_code, second.json()["error"]) == (400, "invalid_token")
    assert fake.revoked == ["rt-1"]


def test_revoking_a_refresh_token_kills_the_access_tokens_it_minted() -> None:
    fake = FakeDrive()
    c = httpx.Client(transport=httpx.MockTransport(fake.handler))
    form = {"grant_type": "refresh_token", "client_id": "cid", "client_secret": "csec"}
    minted = [
        c.post(
            "https://oauth2.googleapis.com/token", data={**form, "refresh_token": "rt-1"}
        ).json()["access_token"]
        for _ in range(2)
    ]
    other = fake.add_file("keep", b"x")
    for token in minted:
        r = c.get(f"{API}/drive/v3/files/{other}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
    assert (
        c.post("https://oauth2.googleapis.com/revoke", params={"token": "rt-1"}).status_code == 200
    )
    for token in minted:
        r = c.get(f"{API}/drive/v3/files/{other}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
        assert token not in fake.access_tokens
    # the refresh token is gone for good
    r = c.post("https://oauth2.googleapis.com/token", data={**form, "refresh_token": "rt-1"})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_grant")


# ----- final review: cut error bodies and a bare final-chunk response ---------------------------


def test_cut_after_with_a_status_cuts_that_error_body_on_any_request() -> None:
    fake = FakeDrive()
    fid = fake.add_file("a", b"0123456789")
    c = client(fake)
    for url, params in (
        (f"{API}/drive/v3/files/{fid}", {"alt": "media"}),
        (f"{API}/drive/v3/files", {}),
    ):
        fake.faults.append(Fault(status=503, method="GET", cut_after=7))
        received = bytearray()
        with c.stream("GET", url, params=params) as r:
            assert r.status_code == 503
            full = int(r.headers["content-length"])
            assert full > 7
            with pytest.raises(httpx.ReadError):
                drain(r, received)
        assert bytes(received) == b'{"error'
    assert fake.faults == []
    fake.faults.append(Fault(status=503, method="GET", cut_after=10_000))  # past the error body
    r = c.get(f"{API}/drive/v3/files")
    assert r.status_code == 400
    assert "cut_after=10000" in r.json()["error"]["message"]
    fake.faults.append(Fault(method="GET"))  # no status, no cut: a plain 503
    assert c.get(f"{API}/drive/v3/files").status_code == 503


def test_bare_final_response_answers_the_last_chunk_with_id_name_and_mime_type_only() -> None:
    fake = FakeDrive()
    fake.bare_final_response = True
    c = client(fake)
    session = start_upload(c, 4)
    r = c.put(session, content=b"abcd", headers={"Content-Range": "bytes 0-3/4"})
    assert r.status_code == 200
    assert set(r.json()) == {"id", "name", "mimeType"}
    # asking the finished session again gives the same bare answer; a plain GET gives it all
    again = c.put(session, headers={"Content-Range": "bytes */4"})
    assert again.json() == r.json()
    full = c.get(f"{API}/drive/v3/files/{r.json()['id']}").json()
    assert (full["size"], full["webViewLink"] != "", full["createdTime"] != "") == ("4", True, True)
