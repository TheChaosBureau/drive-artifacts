# SPDX-License-Identifier: Apache-2.0
"""The six Drive calls this package makes, with retries, resumable uploads and ranged downloads."""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from drive_artifacts.errors import DriveError, NotFoundError
from drive_artifacts.model import FOLDER_MIME, ZIP_MIME
from drive_artifacts.oauth import TokenSource
from drive_artifacts.settings import Settings

FIELDS = "id,name,mimeType,parents,appProperties,size,createdTime,modifiedTime,webViewLink,trashed"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_REASONS = frozenset({"userRateLimitExceeded", "rateLimitExceeded"})
TIMEOUT = httpx.Timeout(30.0, read=120.0)


@dataclass(frozen=True, slots=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...]
    app_properties: dict[str, str]
    size: int | None
    created_time: str
    modified_time: str
    web_view_link: str
    trashed: bool

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> DriveFile:
        parents = data.get("parents")
        props = data.get("appProperties")
        size = data.get("size")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            mime_type=str(data.get("mimeType", "")),
            parents=tuple(str(p) for p in parents) if isinstance(parents, list) else (),
            app_properties={str(k): str(v) for k, v in props.items()}
            if isinstance(props, Mapping)
            else {},
            size=int(str(size)) if size is not None else None,
            created_time=str(data.get("createdTime", "")),
            modified_time=str(data.get("modifiedTime", "")),
            web_view_link=str(data.get("webViewLink", "")),
            trashed=bool(data.get("trashed", False)),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "size": str(self.size or 0),
            "appProperties": dict(self.app_properties),
            "webViewLink": self.web_view_link,
            "createdTime": self.created_time,
            "modifiedTime": self.modified_time,
        }


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 10
    deadline_s: float = 900.0
    base_s: float = 1.0
    cap_s: float = 64.0


DEFAULT_POLICY = RetryPolicy()  # frozen, so one shared instance is a safe default argument


class _Transient(Exception):  # noqa: N818 - internal control flow, never leaves this module
    """A failure the caller may retry; carries the last status and reason."""

    def __init__(self, message: str, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class _Unauthorized(Exception):  # noqa: N818 - internal control flow, never leaves this module
    """Drive answered 401: the access token is stale."""


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("reason", ""))
        return str(error.get("status", ""))
    return ""


def _received(response: httpx.Response) -> int:
    """How many bytes a resumable session holds, from a 308's `Range: bytes=0-N` (absent: none)."""
    rng = response.headers.get("Range")
    if not rng:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", rng.strip())
    if match is None:
        raise DriveError(f"Drive answered an upload chunk with a malformed Range header ({rng!r})")
    return int(match.group(1)) + 1


def _json_file(response: httpx.Response) -> dict[str, object]:
    """The JSON object a successful metadata call must answer with. A proxy or a captive
    portal answers 200 with a page of its own; that is an error, never a file."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        # An upload session's URL carries its id in the query: a credential, so never echoed.
        where = f"{response.request.method} {response.request.url.copy_with(query=None)}"
        raise DriveError(
            f"Drive answered {where} with HTTP {response.status_code} but not the JSON it "
            "should have sent; a proxy or a sign-in page may be in the way",
            status=response.status_code,
        )
    return body


def _file(response: httpx.Response) -> DriveFile:
    body = _json_file(response)
    if "id" not in body:
        where = f"{response.request.method} {response.request.url.copy_with(query=None)}"
        raise DriveError(
            f"Drive answered {where} with HTTP {response.status_code} but not the JSON it "
            "should have sent (no file id)",
            status=response.status_code,
        )
    return DriveFile.from_json(body)


def _size_of(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


class DriveClient:
    def __init__(
        self,
        http: httpx.Client,
        tokens: TokenSource,
        settings: Settings,
        *,
        policy: RetryPolicy = DEFAULT_POLICY,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_size % (256 * 1024) and chunk_size >= 256 * 1024:
            raise ValueError("chunk_size must be a multiple of 256 KiB")
        self._http = http
        self._tokens = tokens
        self._api = settings.api_base
        self._upload = settings.upload_base
        self._policy = policy
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._chunk = chunk_size

    # ----- transport with retries ---------------------------------------------
    def _backoff(self, attempt: int, start: float) -> None:
        """Full-jitter exponential backoff that never sleeps past the deadline counted from `start`."""
        delay = min(self._policy.cap_s, self._policy.base_s * 2 ** (attempt - 1)) * self._rng()
        left = self._policy.deadline_s - (self._clock() - start)
        self._sleep(max(0.0, min(delay, left)))

    def _give_up_if_spent(
        self, what: str, attempt: int, start: float, detail: str, *, status: int, reason: str
    ) -> None:
        """Raise `DriveError` once `attempt` failures, or the time since `start`, use up the policy."""
        if attempt >= self._policy.max_attempts:
            raise DriveError(
                f"{what} after {attempt} attempts: {detail}", status=status, reason=reason
            )
        if self._clock() - start >= self._policy.deadline_s:
            raise DriveError(
                f"{what} at the {self._policy.deadline_s:g} s deadline: {detail}",
                status=status,
                reason=reason,
            )

    def _send_once(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None,
        json: object | None,
        content: bytes | None,
        headers: Mapping[str, str] | None,
        stream: bool,
        allow: frozenset[int],
    ) -> httpx.Response:
        """One attempt. Raises `_Transient` for anything worth retrying."""
        hdrs = {**(headers or {}), "Authorization": f"Bearer {self._tokens.access_token()}"}
        request = self._http.build_request(
            method, url, params=params, json=json, content=content, headers=hdrs, timeout=TIMEOUT
        )
        try:
            response = self._http.send(request, stream=stream)
        except httpx.TransportError as exc:
            raise _Transient(f"transport error: {exc}") from exc
        # 3xx too is a failure: httpx follows no redirects, and a redirect's body is not a file.
        if response.status_code < 300 or response.status_code in allow:
            return response
        if stream:
            try:
                response.read()
            except httpx.TransportError as exc:
                response.close()
                raise _Transient(
                    f"HTTP {response.status_code}, and its body broke off: {exc}",
                    response.status_code,
                ) from exc
        reason = _reason(response)
        if response.status_code == 401:
            raise _Unauthorized(reason)
        # An upload session's URL carries its id in the query: a credential, so never echoed.
        where = f"{method} {httpx.URL(url).copy_with(query=None)}"
        if response.status_code in (404, 410):  # 410: what an expired upload session may answer
            raise NotFoundError(f"Drive has no such item ({where})")
        if response.status_code in RETRYABLE_STATUS or (
            response.status_code == 403 and reason in RATE_LIMIT_REASONS
        ):
            raise _Transient(f"HTTP {response.status_code} {reason}", response.status_code, reason)
        raise DriveError(
            f"Drive refused {where}: HTTP {response.status_code} {reason}",
            status=response.status_code,
            reason=reason,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
        allow: frozenset[int] = frozenset(),
        retry: bool = True,
    ) -> httpx.Response:
        start = self._clock()
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._send_once(
                    method,
                    url,
                    params=params,
                    json=json,
                    content=content,
                    headers=headers,
                    stream=stream,
                    allow=allow,
                )
            except _Unauthorized:
                if refreshed:
                    raise DriveError(
                        "Drive rejected a freshly refreshed access token",
                        status=401,
                        reason="authError",
                    ) from None
                refreshed = True
                self._tokens.access_token(force_refresh=True)
                continue
            except _Transient as exc:
                if not retry:
                    raise
                self._give_up_if_spent(
                    "gave up", attempt, start, str(exc), status=exc.status, reason=exc.reason
                )
                self._backoff(attempt, start)

    # ----- metadata calls -----------------------------------------------------
    def create_folder(
        self, name: str, *, parent: str | None, app_properties: Mapping[str, str]
    ) -> DriveFile:
        body: dict[str, object] = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "appProperties": dict(app_properties),
        }
        if parent:
            body["parents"] = [parent]
        response = self._request("POST", f"{self._api}/files", params={"fields": FIELDS}, json=body)
        return _file(response)

    def list_files(
        self, q: str, *, order_by: str | None = None, page_size: int = 100
    ) -> list[DriveFile]:
        files: list[DriveFile] = []
        token: str | None = None
        while True:
            params = {
                "q": q,
                "pageSize": str(page_size),
                "fields": f"nextPageToken,files({FIELDS})",
                "spaces": "drive",
            }
            if order_by:
                params["orderBy"] = order_by
            if token:
                params["pageToken"] = token
            body = _json_file(self._request("GET", f"{self._api}/files", params=params))
            listed = body.get("files", [])
            files.extend(
                DriveFile.from_json(f) for f in (listed if isinstance(listed, list) else [])
            )
            token = str(body["nextPageToken"]) if body.get("nextPageToken") else None
            if not token:
                return files

    def get_file(self, file_id: str) -> DriveFile:
        response = self._request("GET", f"{self._api}/files/{file_id}", params={"fields": FIELDS})
        return _file(response)

    def trash(self, file_id: str) -> None:
        self._request(
            "PATCH", f"{self._api}/files/{file_id}", params={"fields": "id"}, json={"trashed": True}
        )

    # ----- resumable upload ---------------------------------------------------
    def upload_file(
        self,
        path: Path,
        *,
        name: str,
        parent: str | None,
        app_properties: Mapping[str, str],
        mime: str = ZIP_MIME,
    ) -> DriveFile:
        total = path.stat().st_size
        meta: dict[str, object] = {
            "name": name,
            "mimeType": mime,
            "appProperties": dict(app_properties),
        }
        if parent:
            meta["parents"] = [parent]
        for _restart in range(2):
            opened = self._request(
                "POST",
                f"{self._upload}/files",
                params={"uploadType": "resumable", "fields": FIELDS},
                json=meta,
                headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(total)},
            )
            session = self._session_url(opened)
            try:
                return self._send_chunks(session, path, total, mime)
            except NotFoundError:
                continue  # the session expired: open a new one, once
        raise DriveError(
            "the upload session expired twice; giving up", status=404, reason="notFound"
        )

    def _session_url(self, opened: httpx.Response) -> str:
        """The session's URL from the opening POST. Every chunk carries the bearer token to it, so
        it must be on the upload host (scheme, host and port) and nowhere else."""
        location = opened.headers.get("Location")
        if not location:
            raise DriveError("Drive opened no upload session")
        base = httpx.URL(self._upload)
        # An absolute Location replaces the base; a relative one resolves against it.
        try:
            session = base.join(location)
        except httpx.InvalidURL as exc:
            raise DriveError(f"Drive answered with a malformed upload session URL ({exc})") from exc
        if (session.scheme, session.host, session.port) != (base.scheme, base.host, base.port):
            raise DriveError(
                "upload session URL points at an unexpected host "
                f"({session.scheme}://{session.netloc.decode()}); "
                "refusing to send the access token there"
            )
        return str(session)

    def _session_offset(self, session: str, total: int) -> tuple[int, DriveFile | None]:
        """One attempt at asking the session how much it holds. Raises `_Transient` like a chunk
        does: the caller owns the one retry budget of a stall."""
        response = self._request(
            "PUT",
            session,
            headers={"Content-Range": f"bytes */{total}"},
            allow=frozenset({308}),
            retry=False,
        )
        if response.status_code in (200, 201):
            return total, _file(response)
        return _received(response), None

    def _send_chunks(self, session: str, path: Path, total: int, mime: str) -> DriveFile:
        offset = 0
        # The retry budget is for trouble since the server last advanced, not for the whole
        # upload: a healthy upload can run for hours, and each accepted chunk starts it afresh.
        attempt = 0
        start = self._clock()
        with path.open("rb") as fh:
            while True:
                fh.seek(offset)
                chunk = fh.read(self._chunk)
                end = offset + len(chunk) - 1
                content_range = f"bytes {offset}-{end}/{total}" if chunk else f"bytes */{total}"
                known = True  # whether `reached` is what the server said it holds
                try:
                    response = self._request(
                        "PUT",
                        session,
                        content=chunk,
                        headers={"Content-Range": content_range, "Content-Type": mime},
                        allow=frozenset({308}),
                        retry=False,
                    )
                except _Transient as exc:
                    failure, reached = exc, offset
                    known = False
                else:
                    if response.status_code in (200, 201):
                        return _file(response)
                    reached = _received(response)  # a 308: what the server holds now
                    failure = _Transient(
                        f"the server still holds {reached} bytes", 308, "noProgress"
                    )
                # Until the server is known to hold more than before, this is a stall: every
                # failed chunk and every failed offset query in it draws on one budget. The
                # offset is asked for before the failure is counted, so a chunk the server
                # stored although its answer was lost is progress, not a failure. A chunk is
                # sent again only from an offset the server confirmed.
                while reached <= offset:
                    if not known:
                        try:
                            reached, done = self._session_offset(session, total)
                        except _Transient as exc:
                            failure = exc
                        else:
                            if done is not None:
                                return done
                            known = True
                            if reached > offset:
                                break
                    attempt += 1
                    what = (
                        "upload gave up"
                        if failure.status != 308
                        else "upload is not making progress"
                    )
                    self._give_up_if_spent(
                        what,
                        attempt,
                        start,
                        str(failure),
                        status=failure.status,
                        reason=failure.reason,
                    )
                    self._backoff(attempt, start)
                    if known:
                        break  # the offset is confirmed and unchanged: send the chunk again
                if reached > offset:
                    attempt, start = 0, self._clock()
                offset = reached

    # ----- download -----------------------------------------------------------
    def download_file(self, file_id: str, dest: Path, *, expected_size: int | None = None) -> int:
        """Stream to `dest.part`, resuming with Range after interruptions, then rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        part.unlink(missing_ok=True)
        attempt = 0  # consecutive passes that broke without bringing a byte
        start = self._clock()  # of that run of fruitless passes
        url = f"{self._api}/files/{file_id}"
        try:
            while True:
                offset = _size_of(part)
                if expected_size is not None and part.exists() and offset >= expected_size:
                    break
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                response = self._request(
                    "GET",
                    url,
                    params={"alt": "media"},
                    headers=headers,
                    stream=True,
                    allow=frozenset({416}),
                )
                if response.status_code == 416:
                    # We asked for bytes past the end: Drive has no more than we already hold.
                    response.close()
                    raise DriveError(
                        f"download size mismatch: Drive has only {offset} bytes",
                        status=416,
                        reason="requestedRangeNotSatisfiable",
                    )
                mode = "ab" if offset and response.status_code == 206 else "wb"
                try:
                    with part.open(mode) as fh:
                        for data in response.iter_bytes():
                            fh.write(data)
                except httpx.TransportError as exc:
                    held = _size_of(part)
                    if expected_size is not None and held >= expected_size:
                        continue  # it broke after the last byte: complete, nothing to wait for
                    if held > offset:
                        # it broke, but only after bringing new bytes: progress
                        attempt, start = 0, self._clock()
                    attempt += 1
                    self._give_up_if_spent(
                        "download gave up", attempt, start, str(exc), status=0, reason=""
                    )
                    self._backoff(attempt, start)
                    continue
                finally:
                    response.close()
                size = _size_of(part)
                if expected_size is None or size >= expected_size:
                    break
                if size <= offset:  # a clean response that brought nothing new: Drive disagrees
                    raise DriveError(
                        f"download size mismatch: got {size} bytes, expected {expected_size}"
                    )
                # A clean but short stream that moved forward: ask for the rest. Each such pass
                # adds bytes, so this ends; the no-progress case above is the endless-loop guard.
                attempt = 0
            size = _size_of(part)
            if expected_size is not None and size != expected_size:
                raise DriveError(
                    f"download size mismatch: got {size} bytes, expected {expected_size}"
                )
            part.replace(dest)
            return size
        finally:
            part.unlink(missing_ok=True)
