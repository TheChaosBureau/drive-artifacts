# SPDX-License-Identifier: Apache-2.0
import base64
import hashlib
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from drive_artifacts.errors import AuthError
from drive_artifacts.oauth import (
    RefreshingTokenSource,
    StoredToken,
    build_auth_url,
    exchange_code,
    loopback_login,
    pkce_pair,
    revoke,
)
from drive_artifacts.settings import Settings
from tests.fake_drive import FakeDrive, Fault

SETTINGS = Settings.from_env(
    {"DRIVE_ARTIFACTS_CLIENT_ID": "cid", "DRIVE_ARTIFACTS_CLIENT_SECRET": "csec"}
)
STORED = StoredToken(refresh_token="rt-1", client_id="cid")


def http(fake: FakeDrive) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


def test_stored_token_round_trip_and_validation() -> None:
    text = STORED.dumps()
    assert text == '{"client_id": "cid", "refresh_token": "rt-1", "v": 1}'
    assert StoredToken.parse(text) == STORED
    for bad in [
        "",
        "not json",
        "[]",
        '{"v": 2, "refresh_token": "x", "client_id": "c"}',
        '{"v": 1, "client_id": "c"}',
        '{"v": 1, "refresh_token": "", "client_id": "c"}',
    ]:
        with pytest.raises(AuthError):
            StoredToken.parse(bad)


def test_refreshing_source_caches_until_near_expiry() -> None:
    fake = FakeDrive()
    now = [1000.0]
    seen: list[str] = []
    src = RefreshingTokenSource(
        http(fake), SETTINGS, STORED, clock=lambda: now[0], on_new_token=seen.append
    )
    first = src.access_token()
    assert first == src.access_token()
    now[0] += 3599 - 61  # inside the 60 s safety margin: still cached
    assert first == src.access_token()
    now[0] += 2
    second = src.access_token()
    assert second != first
    assert src.access_token(force_refresh=True) not in (first, second)
    assert seen == [first, second, src.access_token()]
    assert sum(1 for r in fake.requests if r.url.path.endswith("/token")) == 3


def test_missing_expires_in_defaults_to_an_hour() -> None:
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, json={"access_token": f"at-x{calls[0]}"})

    now = [0.0]
    src = RefreshingTokenSource(
        httpx.Client(transport=httpx.MockTransport(handler)), SETTINGS, STORED, clock=lambda: now[0]
    )
    first = src.access_token()
    now[0] = 3600 - 61
    assert src.access_token() == first
    now[0] = 3600 - 59
    assert src.access_token() != first


def test_invalid_grant_is_an_auth_error_that_names_login() -> None:
    fake = FakeDrive()
    fake.refresh_tokens.clear()
    with pytest.raises(AuthError, match="drive-artifacts login"):
        RefreshingTokenSource(http(fake), SETTINGS, STORED).access_token()


def test_client_mismatch_is_rejected_up_front() -> None:
    with pytest.raises(AuthError) as excinfo:
        RefreshingTokenSource(http(FakeDrive()), SETTINGS, StoredToken("rt-1", "other-client"))
    message = str(excinfo.value)
    assert "'other-client'" in message  # the client that minted the token
    assert "'cid'" in message  # the client this run is configured for


def test_stored_token_repr_keeps_the_refresh_token_out() -> None:
    token = StoredToken(refresh_token="rt-very-secret", client_id="cid")
    assert "rt-very-secret" not in repr(token)
    assert "rt-very-secret" not in str(token)
    assert "cid" in repr(token)


def test_token_endpoint_5xx_is_retried_then_fails() -> None:
    fake = FakeDrive()
    fake.faults.append(Fault(status=503, path="/token", count=2))
    sleeps: list[float] = []
    src = RefreshingTokenSource(http(fake), SETTINGS, STORED, sleep=sleeps.append)
    assert src.access_token().startswith("at-")
    assert len(sleeps) == 2
    fake.faults.append(Fault(status=503, path="/token", count=10))
    with pytest.raises(AuthError, match="503"):
        src.access_token(force_refresh=True)


def test_pkce_pair_is_s256() -> None:
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert challenge == expected


def test_auth_url_requests_offline_drive_file_only() -> None:
    url = build_auth_url(SETTINGS, redirect_uri="http://127.0.0.1:5/", state="st", challenge="ch")
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q == {
        "client_id": "cid",
        "redirect_uri": "http://127.0.0.1:5/",
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/drive.file",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "state": "st",
    }


def test_exchange_and_revoke() -> None:
    fake = FakeDrive()
    verifier, challenge = pkce_pair()
    code = fake.issue_auth_code(challenge)
    stored = exchange_code(
        http(fake), SETTINGS, code=code, verifier=verifier, redirect_uri="http://127.0.0.1:1/"
    )
    assert stored.client_id == "cid"
    assert stored.refresh_token in fake.refresh_tokens
    revoke(http(fake), SETTINGS, stored.refresh_token)
    assert stored.refresh_token in fake.revoked
    # already revoked: Google answers 400 invalid_token, which is fine
    revoke(http(fake), SETTINGS, stored.refresh_token)
    fake.faults.append(Fault(status=503, path="/revoke"))
    with pytest.raises(AuthError, match="503"):
        revoke(http(fake), SETTINGS, "rt-1")
    with pytest.raises(AuthError) as excinfo:
        exchange_code(
            http(fake),
            SETTINGS,
            code="bogus",
            verifier=verifier,
            redirect_uri="http://127.0.0.1:1/",
        )
    # a rejected code is about this login, not about a stored secret
    assert "authorization code was rejected" in str(excinfo.value)
    assert "drive-artifacts login" in str(excinfo.value)
    assert "DRIVE_ARTIFACTS_TOKEN" not in str(excinfo.value)


def test_revoke_keeps_the_refresh_token_out_of_the_url() -> None:
    # httpx logs every request URL at INFO, so a token in the query would reach the logs
    fake = FakeDrive()
    revoke(http(fake), SETTINGS, "rt-1")
    assert fake.revoked == ["rt-1"]  # the form body reached the endpoint
    assert [r for r in fake.requests if r.url.path.endswith("/revoke")]
    assert all("rt-1" not in str(r.url) for r in fake.requests)


def _browser(
    fake: FakeDrive, *, state_override: str | None = None, deny: bool = False
) -> Callable[[str], None]:
    def open_browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = q["redirect_uri"]
        if deny:
            params = {"error": "access_denied", "state": q["state"]}
        else:
            params = {
                "code": fake.issue_auth_code(q["code_challenge"]),
                "state": state_override or q["state"],
            }
        threading.Thread(
            target=lambda: httpx.get(redirect, params=params, timeout=5), daemon=True
        ).start()

    return open_browser


def test_loopback_login_happy_path() -> None:
    fake = FakeDrive()
    stored = loopback_login(http(fake), SETTINGS, _browser(fake), timeout=10)
    assert stored.refresh_token in fake.refresh_tokens
    assert stored.client_id == "cid"


def test_loopback_login_rejects_wrong_state_and_denial() -> None:
    fake = FakeDrive()
    # a response with the wrong state is refused and ignored, so the login ends in a timeout
    with pytest.raises(AuthError, match="state"):
        loopback_login(http(fake), SETTINGS, _browser(fake, state_override="evil"), timeout=1)
    with pytest.raises(AuthError, match="access_denied"):
        loopback_login(http(fake), SETTINGS, _browser(fake, deny=True), timeout=10)


def test_loopback_login_times_out() -> None:
    with pytest.raises(AuthError, match="timed out"):
        loopback_login(http(FakeDrive()), SETTINGS, lambda _url: None, timeout=0.5)


def test_loopback_login_times_out_on_a_connection_that_sends_nothing() -> None:
    held: list[socket.socket] = []

    def open_browser(url: str) -> None:
        redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
        # connects, then stays silent and open: only a bounded read can end the wait
        held.append(socket.create_connection(("127.0.0.1", urlparse(redirect).port or 0)))

    started = time.monotonic()
    try:
        with pytest.raises(AuthError, match="timed out"):
            loopback_login(http(FakeDrive()), SETTINGS, open_browser, timeout=0.5)
        assert time.monotonic() - started < 3
    finally:
        for sock in held:
            sock.close()


def test_loopback_login_ignores_stray_requests_until_the_real_redirect() -> None:
    fake = FakeDrive()

    def open_browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = q["redirect_uri"]

        def visit() -> None:
            httpx.get(f"{redirect}favicon.ico", timeout=5)  # a probe: no state at all
            httpx.get(redirect, params={"code": "stolen", "state": "evil"}, timeout=5)
            httpx.post(redirect, params={"code": "posted", "state": q["state"]}, timeout=5)
            real = {"code": fake.issue_auth_code(q["code_challenge"]), "state": q["state"]}
            httpx.get(redirect, params=real, timeout=5)

        threading.Thread(target=visit, daemon=True).start()

    stored = loopback_login(http(fake), SETTINGS, open_browser, timeout=10)
    assert stored.refresh_token in fake.refresh_tokens


def test_loopback_login_without_a_code_is_refused() -> None:
    def open_browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        params = {"state": q["state"]}
        threading.Thread(
            target=lambda: httpx.get(q["redirect_uri"], params=params, timeout=5), daemon=True
        ).start()

    with pytest.raises(AuthError, match="no authorization code"):
        loopback_login(http(FakeDrive()), SETTINGS, open_browser, timeout=10)


def _canned(response: httpx.Response) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _request: response))


def test_token_endpoint_transport_errors_are_retried_then_fail() -> None:
    fake = FakeDrive()
    fake.faults.append(Fault(drop=True, path="/token", count=1))
    sleeps: list[float] = []
    src = RefreshingTokenSource(http(fake), SETTINGS, STORED, sleep=sleeps.append)
    assert src.access_token().startswith("at-")
    assert sleeps == [1.0]
    fake.faults.append(Fault(drop=True, path="/token", count=10))
    with pytest.raises(AuthError, match="ConnectError"):
        src.access_token(force_refresh=True)


def _undecodable(request: httpx.Request) -> httpx.Response:
    raise httpx.DecodingError("bad gzip", request=request)


def test_undecodable_token_responses_are_auth_errors_not_httpx_errors() -> None:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(_undecodable))
    src = RefreshingTokenSource(client, SETTINGS, STORED, sleep=sleeps.append)
    with pytest.raises(AuthError, match="DecodingError"):
        src.access_token()
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"access_token": None}),
        httpx.Response(200, json={"access_token": ""}),
        httpx.Response(200, json={"access_token": 7}),
        httpx.Response(401, json={"error": "invalid_client"}),
    ],
)
def test_unusable_token_responses_are_auth_errors(response: httpx.Response) -> None:
    src = RefreshingTokenSource(_canned(response), SETTINGS, STORED)
    with pytest.raises(AuthError, match="refused the token request"):
        src.access_token()


def test_exchange_without_a_refresh_token_is_refused() -> None:
    client = _canned(httpx.Response(200, json={"access_token": "at-x"}))
    with pytest.raises(AuthError, match="refresh token"):
        exchange_code(client, SETTINGS, code="c", verifier="v", redirect_uri="http://127.0.0.1:1/")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(400, json=["invalid_token"]),
        httpx.Response(400, json={"error": "invalid_request"}),
    ],
)
def test_revoke_refuses_everything_but_success_and_invalid_token(response: httpx.Response) -> None:
    with pytest.raises(AuthError, match="refused to revoke"):
        revoke(_canned(response), SETTINGS, "rt-1")


def test_revoke_wraps_transport_errors() -> None:
    fake = FakeDrive()
    fake.faults.append(Fault(drop=True, path="/revoke"))
    with pytest.raises(AuthError, match="revoke request failed"):
        revoke(http(fake), SETTINGS, "rt-1")


def test_revoke_wraps_undecodable_responses() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_undecodable))
    with pytest.raises(AuthError, match="DecodingError"):
        revoke(client, SETTINGS, "rt-1")
