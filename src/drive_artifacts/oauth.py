# SPDX-License-Identifier: Apache-2.0
"""Google OAuth for an installed app: refresh tokens, PKCE loopback login, revoke.

The refresh token is the only durable secret. Access tokens live in memory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from drive_artifacts.errors import AuthError, UsageError
from drive_artifacts.settings import SCOPE, Settings

REFRESH_MARGIN_S = 60.0
DEFAULT_LIFETIME_S = 3600.0
LOGIN_HINT = "run `drive-artifacts login` again and update the DRIVE_ARTIFACTS_TOKEN secret"


@dataclass(frozen=True, slots=True)
class StoredToken:
    """What goes into the repository secret."""

    refresh_token: str = field(repr=False)
    client_id: str
    v: int = 1

    @classmethod
    def parse(cls, text: str) -> StoredToken:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuthError(f"DRIVE_ARTIFACTS_TOKEN is not JSON; {LOGIN_HINT}") from exc
        if not isinstance(data, dict) or data.get("v") != 1:
            raise AuthError(f"DRIVE_ARTIFACTS_TOKEN has an unknown format; {LOGIN_HINT}")
        refresh, client = data.get("refresh_token"), data.get("client_id")
        if not isinstance(refresh, str) or not refresh or not isinstance(client, str) or not client:
            raise AuthError(f"DRIVE_ARTIFACTS_TOKEN is incomplete; {LOGIN_HINT}")
        return cls(refresh_token=refresh, client_id=client)

    def dumps(self) -> str:
        return json.dumps(
            {"client_id": self.client_id, "refresh_token": self.refresh_token, "v": self.v},
            sort_keys=True,
        )


class TokenSource(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


class RefreshingTokenSource:
    """Trades the refresh token for access tokens and caches them until near expiry."""

    def __init__(
        self,
        http: httpx.Client,
        settings: Settings,
        stored: StoredToken,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_new_token: Callable[[str], None] | None = None,
    ) -> None:
        if stored.client_id != settings.client_id:
            raise AuthError(
                f"the token was minted by client {stored.client_id!r} but this run is configured for "
                f"{settings.client_id!r}; set DRIVE_ARTIFACTS_CLIENT_ID/SECRET to match or {LOGIN_HINT}"
            )
        self._http = http
        self._settings = settings
        self._stored = stored
        self._clock = clock
        self._sleep = sleep
        self._on_new_token = on_new_token
        self._token: str | None = None
        self._expires_at = 0.0

    def access_token(self, *, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._token
            and self._clock() < self._expires_at - REFRESH_MARGIN_S
        ):
            return self._token
        body = _token_request(
            self._http,
            self._settings,
            {"grant_type": "refresh_token", "refresh_token": self._stored.refresh_token},
            invalid_grant=f"the Drive grant was revoked or expired (invalid_grant); {LOGIN_HINT}",
            sleep=self._sleep,
        )
        self._token = str(body["access_token"])
        lifetime = body.get("expires_in")
        self._expires_at = self._clock() + (
            float(lifetime) if isinstance(lifetime, int | float) else DEFAULT_LIFETIME_S
        )
        if self._on_new_token is not None:
            self._on_new_token(self._token)
        return self._token


def _token_request(
    http: httpx.Client,
    settings: Settings,
    form: dict[str, str],
    *,
    invalid_grant: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """POST a token request. `invalid_grant` is the message for Google's answer of that name:
    what it means (and what to do) differs between a refresh and a code exchange."""
    data = {**form, "client_id": settings.client_id, "client_secret": settings.client_secret}
    last = "no response"
    for attempt in range(3):
        try:
            response = http.post(settings.token_url, data=data, timeout=30)
        except httpx.RequestError as exc:  # transport failures, undecodable bodies, redirect loops
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code < 500:
                return _token_response(response, invalid_grant=invalid_grant)
            last = f"HTTP {response.status_code}"
        if attempt < 2:
            sleep(2.0**attempt)
    raise AuthError(f"Google's token endpoint failed after 3 attempts ({last})")


def _token_response(response: httpx.Response, *, invalid_grant: str) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError:
        body = {}
    access = body.get("access_token") if isinstance(body, dict) else None
    if response.status_code == 200 and isinstance(access, str) and access:
        result: dict[str, object] = body
        return result
    error = body.get("error", "unknown_error") if isinstance(body, dict) else "unknown_error"
    if error == "invalid_grant":
        raise AuthError(invalid_grant)
    raise AuthError(f"Google refused the token request: {error} (HTTP {response.status_code})")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_auth_url(settings: Settings, *, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{settings.auth_url}?{urlencode(params)}"


def exchange_code(
    http: httpx.Client, settings: Settings, *, code: str, verifier: str, redirect_uri: str
) -> StoredToken:
    body = _token_request(
        http,
        settings,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
        invalid_grant=(
            "the authorization code was rejected (invalid_grant); run `drive-artifacts login` again"
        ),
    )
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthError(
            "Google did not return a refresh token; revoke the app at "
            "myaccount.google.com/permissions and log in again"
        )
    return StoredToken(refresh_token=refresh, client_id=settings.client_id)


def revoke(http: httpx.Client, settings: Settings, token: str) -> None:
    """Revoke the grant at Google. An already-revoked or unknown token is not an error: the goal is reached."""
    try:
        # The token goes in the form body (RFC 7009), never the URL: httpx logs request URLs at
        # INFO and proxies record them.
        response = http.post(settings.revoke_url, data={"token": token}, timeout=30)
    except httpx.RequestError as exc:
        raise AuthError(f"the revoke request failed ({type(exc).__name__}: {exc})") from exc
    if response.status_code == 200:
        return
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if response.status_code == 400 and error == "invalid_token":
        return
    raise AuthError(
        f"Google refused to revoke the token (HTTP {response.status_code} {error or ''})".rstrip()
    )


def loopback_login(
    http: httpx.Client,
    settings: Settings,
    open_browser: Callable[[str], None],
    *,
    timeout: float = 300.0,
) -> StoredToken:
    """Authorization-code flow with PKCE on a loopback redirect, as Google prescribes for desktop apps."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}  # the one request that carried our state
    wrong_state = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            pass

        def do_GET(self) -> None:
            nonlocal wrong_state
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            presented = query.get("state")
            if presented is None or not secrets.compare_digest(presented.encode(), state.encode()):
                # A favicon probe, a stale tab or something hostile: answer it, keep waiting.
                if presented is not None:
                    wrong_state += 1
                self.send_error(404)
                return
            received.update(query)
            page = b"<html><body><p>drive-artifacts: you can close this tab.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:
        raise UsageError(f"could not open a local port for the login: {exc}") from exc
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
        open_browser(
            build_auth_url(settings, redirect_uri=redirect_uri, state=state, challenge=challenge)
        )
        # `server.timeout` only bounds the wait for a connection. Once one arrives, the handler's
        # own `timeout` bounds each socket read, so a client that connects and says nothing
        # cannot hold the login past the deadline. Requests that do not carry our state are
        # answered and ignored; the loop ends on the one that does, or on the deadline.
        deadline = time.monotonic() + timeout
        while not received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            Handler.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if not received:
        detail = "; a response carrying the wrong state was refused" if wrong_state else ""
        raise AuthError(
            f"login timed out after {timeout:g} s without a valid response from the browser{detail}"
        )
    if "error" in received:
        raise AuthError(f"Google reported {received['error']} during login")
    code = received.get("code")
    if not code:
        raise AuthError("login response carried no authorization code")
    return exchange_code(http, settings, code=code, verifier=verifier, redirect_uri=redirect_uri)
