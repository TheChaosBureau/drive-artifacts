# SPDX-License-Identifier: Apache-2.0
"""An in-memory Google Drive + OAuth token endpoint, faithful to the parts we use.

Quirks modelled on purpose: duplicate names, pagination, appProperties queries,
trash, resumable sessions with 308 offsets, byte-range downloads, access-token
expiry and revocation, invalid_grant, PKCE on the code exchange, and scheduled
faults (403/429/5xx, dropped connections, chunks only partly received, downloads
and error bodies cut mid-stream).

A fault that has no meaning for the request it matches (``truncate_to`` on
anything but a resumable PUT, ``cut_after`` without a ``status`` on anything but
an ``alt=media`` GET, ``cut_after`` beyond the end of the body) answers 400 with a
message saying so, instead of being quietly misrouted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx

FOLDER = "application/vnd.google-apps.folder"


@dataclass
class Fault:
    """A scheduled failure.

    ``drop`` raises ``httpx.ConnectError`` (the server hangs up). ``truncate_to`` keeps
    that many bytes of a resumable chunk PUT and answers 308. ``cut_after`` lets the
    first N bytes of an ``alt=media`` body through, then fails the stream; given an
    explicit ``status`` as well, it cuts that error response's body instead, on any
    request. Otherwise the fault answers ``status`` (503 when unset).
    """

    status: int | None = None
    method: str | None = None
    path: str | None = None
    count: int = 1
    drop: bool = False
    truncate_to: int | None = None
    reason: str = "backendError"
    cut_after: int | None = None

    def matches(self, request: httpx.Request) -> bool:
        if self.method and request.method != self.method:
            return False
        return not self.path or self.path in str(request.url)


class CutStream(httpx.SyncByteStream):
    """A response body that delivers ``full[:cut]`` and then fails with ``httpx.ReadError``.

    ``fake_server`` recognises it and, instead of iterating, writes the same head over
    the socket (under the full Content-Length) and hangs up.
    """

    def __init__(self, full: bytes, cut: int, request: httpx.Request) -> None:
        self.full = full
        self.cut = cut
        self._request = request

    def __iter__(self) -> Iterator[bytes]:
        if self.cut:
            yield self.full[: self.cut]
        raise httpx.ReadError("injected cut", request=self._request)


@dataclass
class FakeFile:
    id: str
    name: str
    mime_type: str
    parents: list[str]
    app_properties: dict[str, str]
    trashed: bool
    content: bytes
    created_time: str
    modified_time: str

    def to_json(self) -> dict[str, object]:
        body: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "mimeType": self.mime_type,
            "parents": list(self.parents),
            "appProperties": dict(self.app_properties),
            "trashed": self.trashed,
            "createdTime": self.created_time,
            "modifiedTime": self.modified_time,
            "webViewLink": f"https://drive.google.com/file/d/{self.id}/view",
        }
        if self.mime_type != FOLDER:
            body["size"] = str(len(self.content))
        return body


@dataclass
class _Session:
    metadata: dict[str, object]
    total: int | None
    received: bytearray = field(default_factory=bytearray)
    expired: bool = False
    file_id: str | None = None


_TERM = re.compile(
    r"appProperties has \{ key='((?:\\.|[^'\\])*)' and value='((?:\\.|[^'\\])*)' \}"
    r"|trashed = (true|false)"
    r"|mimeType (=|!=) '((?:\\.|[^'\\])*)'"
    r"|'((?:\\.|[^'\\])*)' in parents"
    r"|name = '((?:\\.|[^'\\])*)'"
)


_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_AND = re.compile(r"\s+and\s+")


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _is_chunk_put(request: httpx.Request) -> bool:
    return request.method == "PUT" and "/upload/drive/v3/files" in request.url.path


def _is_media_get(request: httpx.Request) -> bool:
    return request.method == "GET" and request.url.params.get("alt") == "media"


def _unsatisfiable(size: int) -> httpx.Response:
    response = _error(416, "requestedRangeNotSatisfiable", "Requested range not satisfiable")
    response.headers["Content-Range"] = f"bytes */{size}"
    return response


def _error(status: int, reason: str, message: str) -> httpx.Response:
    body = {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"domain": "global", "reason": reason, "message": message}],
        }
    }
    return httpx.Response(status, json=body)


def _cut(response: httpx.Response, cut: int, request: httpx.Request) -> httpx.Response:
    """``response`` under its full Content-Length, its body failing after ``cut`` bytes."""
    body = response.content
    if not 0 <= cut < len(body):
        return _error(
            400,
            "badRequest",
            f"injected cut_after={cut} does not fall inside the {len(body)}-byte body",
        )
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    headers["Content-Length"] = str(len(body))
    return httpx.Response(
        response.status_code, headers=headers, stream=CutStream(body, cut, request)
    )


class FakeDrive:
    def __init__(self, client_id: str = "cid", client_secret: str = "csec") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.files: dict[str, FakeFile] = {}
        self.sessions: dict[str, _Session] = {}
        self.faults: list[Fault] = []
        self.requests: list[httpx.Request] = []
        self.refresh_tokens: set[str] = {"rt-1"}
        self.revoked: list[str] = []
        self.access_tokens: set[str] = {"at-1"}
        self.auth_codes: dict[str, str] = {}  # code -> code_challenge
        # Answer the last chunk of an upload with only id, name and mimeType: what Drive sends
        # when the session's `fields` does not reach the final response.
        self.bare_final_response = False
        self._minted: dict[str, str] = {}  # access token -> the refresh token that minted it
        self._clock = datetime(2026, 10, 11, tzinfo=UTC)
        self._counter = 0

    # ----- helpers for tests -------------------------------------------------
    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _now(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def add_folder(
        self, name: str, *, parent: str | None = None, props: dict[str, str] | None = None
    ) -> str:
        return self._create(
            {
                "name": name,
                "mimeType": FOLDER,
                "parents": [parent] if parent else [],
                "appProperties": props or {},
            },
            b"",
        ).id

    def add_file(
        self,
        name: str,
        content: bytes,
        *,
        parent: str | None = None,
        props: dict[str, str] | None = None,
        mime: str = "application/zip",
    ) -> str:
        return self._create(
            {
                "name": name,
                "mimeType": mime,
                "parents": [parent] if parent else [],
                "appProperties": props or {},
            },
            content,
        ).id

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    def expire_session(self, upload_id: str) -> None:
        self.sessions[upload_id].expired = True

    def issue_auth_code(self, code_challenge: str) -> str:
        code = self._next("code-")
        self.auth_codes[code] = code_challenge
        return code

    def live_files(self) -> list[FakeFile]:
        return [f for f in self.files.values() if not f.trashed]

    def _create(self, meta: dict[str, object], content: bytes) -> FakeFile:
        now = self._now()
        parents = meta.get("parents") or []
        props = meta.get("appProperties") or {}
        f = FakeFile(
            id=self._next("id-"),
            name=str(meta.get("name", "Untitled")),
            mime_type=str(meta.get("mimeType", "application/octet-stream")),
            parents=[str(p) for p in parents] if isinstance(parents, list) else [],
            app_properties={str(k): str(v) for k, v in props.items()}
            if isinstance(props, dict)
            else {},
            trashed=False,
            content=content,
            created_time=now,
            modified_time=now,
        )
        self.files[f.id] = f
        return f

    # ----- the handler ---------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        cut_after: int | None = None
        for fault in list(self.faults):
            if not fault.matches(request):
                continue
            fault.count -= 1
            if fault.count <= 0:
                self.faults.remove(fault)
            if fault.drop:
                raise httpx.ConnectError("injected", request=request)
            if fault.truncate_to is not None:
                if not _is_chunk_put(request):
                    return _error(
                        400,
                        "badRequest",
                        "injected truncate_to fault matched a request that is not a resumable "
                        f"upload PUT: {request.method} {request.url.path}",
                    )
                return self._put_chunk(request, truncate_to=fault.truncate_to)
            if fault.cut_after is not None and fault.status is not None:
                return _cut(
                    _error(fault.status, fault.reason, f"injected {fault.status}"),
                    fault.cut_after,
                    request,
                )
            if fault.cut_after is not None:
                if not _is_media_get(request):
                    return _error(
                        400,
                        "badRequest",
                        "injected cut_after fault matched a request that is not an alt=media "
                        f"GET: {request.method} {request.url.path}",
                    )
                cut_after = fault.cut_after
                break  # the download itself is served below, cut short
            status = 503 if fault.status is None else fault.status
            return _error(status, fault.reason, f"injected {status}")
        path = request.url.path
        if path.endswith("/token"):
            return self._token(request)
        if path.endswith("/revoke"):
            return self._revoke(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") not in self.access_tokens:
            return _error(401, "authError", "Invalid Credentials")
        if "/upload/drive/v3/files" in path:
            if request.method == "POST":
                return self._start_session(request)
            if request.method == "PUT":
                return self._put_chunk(request, truncate_to=None)
        m = re.search(r"/drive/v3/files(?:/([^/]+))?$", path)
        if m is None:
            return _error(404, "notFound", f"no route {path}")
        file_id = m.group(1)
        if file_id is None and request.method == "POST":
            return httpx.Response(
                200, json=self._create(json.loads(request.content), b"").to_json()
            )
        if file_id is None and request.method == "GET":
            return self._list(request)
        if file_id is None:
            return _error(405, "methodNotAllowed", request.method)
        f = self.files.get(file_id)
        if f is None:
            return _error(404, "notFound", f"File not found: {file_id}.")
        if _is_media_get(request):
            return self._media(request, f, cut_after=cut_after)
        if request.method == "GET":
            return httpx.Response(200, json=f.to_json())
        if request.method == "PATCH":
            body = json.loads(request.content)
            if "trashed" in body:
                f.trashed = bool(body["trashed"])
            if "name" in body:
                f.name = str(body["name"])
            f.modified_time = self._now()
            return httpx.Response(200, json=f.to_json())
        return _error(405, "methodNotAllowed", request.method)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if (
            form.get("client_id") != self.client_id
            or form.get("client_secret") != self.client_secret
        ):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in self.refresh_tokens:
                return httpx.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "error_description": "Token has been expired or revoked.",
                    },
                )
            at = self._next("at-")
            self.access_tokens.add(at)
            self._minted[at] = form["refresh_token"]
            return httpx.Response(
                200,
                json={
                    "access_token": at,
                    "expires_in": 3599,
                    "token_type": "Bearer",
                    "scope": "https://www.googleapis.com/auth/drive.file",
                },
            )
        if form.get("grant_type") == "authorization_code":
            challenge = self.auth_codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            expected = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if challenge is None or challenge != expected or not form.get("redirect_uri"):
                return httpx.Response(
                    400,
                    json={"error": "invalid_grant", "error_description": "Bad code or verifier."},
                )
            rt, at = self._next("rt-"), self._next("at-")
            self.refresh_tokens.add(rt)
            self.access_tokens.add(at)
            self._minted[at] = rt
            return httpx.Response(
                200,
                json={
                    "access_token": at,
                    "refresh_token": rt,
                    "expires_in": 3599,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token")
        if token is None:
            token = parse_qs(request.content.decode()).get("token", [""])[0]
        if token not in self.refresh_tokens:
            return httpx.Response(
                400,
                json={"error": "invalid_token", "error_description": "Token expired or revoked"},
            )
        self.refresh_tokens.discard(token)
        self.revoked.append(token)
        for access, minted_by in list(self._minted.items()):
            if minted_by == token:
                self.access_tokens.discard(access)
                del self._minted[access]
        return httpx.Response(200, json={})

    def _list(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        pos = 0
        predicates: list[tuple[str, str, str]] = []
        for m in _TERM.finditer(q):
            gap = q[pos : m.start()]
            # nothing before the first term, exactly "and" between any two
            if not (_AND.fullmatch(gap) if predicates else not gap.strip()):
                return _error(400, "invalid", f"Invalid query: {q!r}")
            pos = m.end()
            if m.group(1) is not None:
                predicates.append(("prop", _unescape(m.group(1)), _unescape(m.group(2))))
            elif m.group(3) is not None:
                predicates.append(("trashed", m.group(3), ""))
            elif m.group(4) is not None:
                predicates.append(("mime", m.group(4), _unescape(m.group(5))))
            elif m.group(6) is not None:
                predicates.append(("parent", _unescape(m.group(6)), ""))
            else:
                predicates.append(("name", _unescape(m.group(7)), ""))
        if q[pos:].strip() or (q.strip() and not predicates):
            return _error(400, "invalid", f"Invalid query: {q!r}")

        def ok(f: FakeFile) -> bool:
            for kind, a, b in predicates:
                if kind == "prop" and f.app_properties.get(a) != b:
                    return False
                if kind == "trashed" and f.trashed != (a == "true"):
                    return False
                if kind == "mime" and ((f.mime_type == b) != (a == "=")):
                    return False
                if kind == "parent" and a not in f.parents:
                    return False
                if kind == "name" and f.name != a:
                    return False
            return True

        rows = [f for f in self.files.values() if ok(f)]
        order_by = request.url.params.get("orderBy", "")
        if order_by.startswith("modifiedTime"):
            rows.sort(key=lambda f: f.modified_time, reverse=order_by.endswith("desc"))
        elif order_by.startswith("createdTime"):
            rows.sort(key=lambda f: f.created_time, reverse=order_by.endswith("desc"))
        size_text = request.url.params.get("pageSize", "100")
        start_text = request.url.params.get("pageToken", "0")
        if not (size_text.isascii() and size_text.isdigit() and int(size_text) >= 1):
            return _error(400, "invalid", f"Invalid pageSize: {size_text!r}")
        if not (start_text.isascii() and start_text.isdigit()):
            return _error(400, "invalid", f"Invalid pageToken: {start_text!r}")
        size, start = int(size_text), int(start_text)
        page = rows[start : start + size]
        body: dict[str, object] = {"files": [f.to_json() for f in page]}
        if start + size < len(rows):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)

    def _media(
        self, request: httpx.Request, f: FakeFile, *, cut_after: int | None
    ) -> httpx.Response:
        size = len(f.content)
        status = 200
        data = f.content
        headers: dict[str, str] = {}
        spec = request.headers.get("Range")
        if spec is not None:
            # a single range: bytes=a-b, bytes=a- or the suffix form bytes=-n
            m = _RANGE.fullmatch(spec.strip())
            if m is None or not (m.group(1) or m.group(2)):
                return _error(400, "badRequest", f"unsupported Range header {spec!r}")
            if not m.group(1):
                count = int(m.group(2))
                if count == 0 or size == 0:
                    return _unsatisfiable(size)
                first, last = max(size - count, 0), size - 1
            else:
                first = int(m.group(1))
                if m.group(2) and int(m.group(2)) < first:
                    return _error(400, "badRequest", f"malformed Range header {spec!r}")
                if first >= size:
                    return _unsatisfiable(size)
                last = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            status = 206
            data = f.content[first : last + 1]
            headers["Content-Range"] = f"bytes {first}-{last}/{size}"
        response = httpx.Response(status, content=data, headers=headers)
        return response if cut_after is None else _cut(response, cut_after, request)

    def _start_session(self, request: httpx.Request) -> httpx.Response:
        meta = json.loads(request.content) if request.content else {}
        length = request.headers.get("X-Upload-Content-Length")
        upload_id = self._next("upload-")
        self.sessions[upload_id] = _Session(metadata=meta, total=int(length) if length else None)
        base = f"{request.url.scheme}://{request.url.netloc.decode()}{request.url.path}"
        return httpx.Response(
            200, headers={"Location": f"{base}?uploadType=resumable&upload_id={upload_id}"}
        )

    def _put_chunk(self, request: httpx.Request, *, truncate_to: int | None) -> httpx.Response:
        session = self.sessions.get(request.url.params.get("upload_id", ""))
        if session is None or session.expired:
            return _error(404, "notFound", "upload session not found")
        if session.file_id is not None:
            return self._finished(self.files[session.file_id])
        header = request.headers.get("Content-Range", "")
        m = re.fullmatch(r"bytes (\*|(\d+)-(\d+))/(\d+|\*)", header)
        if m is None:
            return _error(400, "badRequest", f"bad Content-Range {header!r}")
        total = session.total if m.group(4) == "*" else int(m.group(4))
        if m.group(1) != "*":
            start, last = int(m.group(2)), int(m.group(3))
            body = request.content
            if last < start or last != start + len(body) - 1:
                return _error(
                    400,
                    "badRequest",
                    f"Content-Range {header!r} does not match the {len(body)}-byte body",
                )
            if total is not None and last >= total:
                return _error(
                    400, "badRequest", f"Content-Range {header!r} ends beyond the total {total}"
                )
            if start != len(session.received):
                return _error(
                    400,
                    "badRequest",
                    f"chunk starts at {start}, server has {len(session.received)}",
                )
            session.received += body if truncate_to is None else body[:truncate_to]
        session.total = total
        if session.total is not None and len(session.received) >= session.total:
            f = self._create(session.metadata, bytes(session.received[: session.total]))
            session.file_id = f.id
            return self._finished(f)
        headers = {"Range": f"bytes=0-{len(session.received) - 1}"} if session.received else {}
        return httpx.Response(308, headers=headers)

    def _finished(self, f: FakeFile) -> httpx.Response:
        body = f.to_json()
        if self.bare_final_response:
            body = {k: body[k] for k in ("id", "name", "mimeType")}
        return httpx.Response(200, json=body)
