# SPDX-License-Identifier: Apache-2.0
"""The CLI as a child process, talking HTTP to the fake. This module does not import the package directly."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import TOKEN_JSON
from tests.fake_drive import FakeDrive
from tests.fake_server import serve

MASK = "::add-mask::"
# A literal on purpose: this module never imports the package. `test_drive.py` pins that the
# client's default is this value.
CHUNK = 8 * 1024 * 1024


@pytest.fixture
def base() -> Iterator[tuple[FakeDrive, str]]:
    fake = FakeDrive()
    with serve(fake) as url:
        yield fake, url


def _env(url: str, tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),  # Windows needs it to start Python
        # Windows tempfile needs these; POSIX uses TMPDIR
        **{k: os.environ[k] for k in ("TEMP", "TMP", "TMPDIR") if k in os.environ},
        # empty under uv (the package is installed); set when run from a bare checkout
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_REPOSITORY_ID": "42",
        "GITHUB_RUN_ID": "7",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "f" * 40,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "DRIVE_ARTIFACTS_API_BASE": f"{url}/api",
        "DRIVE_ARTIFACTS_OAUTH_BASE": f"{url}/oauth",
        "DRIVE_ARTIFACTS_CLIENT_ID": "cid",
        "DRIVE_ARTIFACTS_CLIENT_SECRET": "csec",
        "DRIVE_ARTIFACTS_TOKEN": TOKEN_JSON,
        **extra,
    }


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "drive_artifacts", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


def _masks(stderr: str) -> list[str]:
    return [line.removeprefix(MASK) for line in stderr.splitlines() if line.startswith(MASK)]


def _unmasked(stderr: str) -> str:
    return "\n".join(line for line in stderr.splitlines() if not line.startswith(MASK))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _fails(result: subprocess.CompletedProcess[str], code: int, fragment: str) -> None:
    """The process exited with `code` and said why on an ::error:: line, with no traceback."""
    assert result.returncode == code, result.stderr
    assert "Traceback" not in result.stderr
    assert any(
        line.startswith("::error::") and fragment in line for line in result.stderr.splitlines()
    ), result.stderr


def test_upload_and_download_round_trip_through_a_real_socket(
    base: tuple[FakeDrive, str], tmp_path: Path
) -> None:
    fake, url = base
    work = tmp_path / "work"
    (work / "dist").mkdir(parents=True)
    # Incompressible bytes, stored (level 0): the zip is bigger than one 8 MiB chunk, so the
    # resumable upload has to send at least two PUTs.
    payload = random.Random(0).randbytes(10_000_000)
    (work / "dist" / "big.bin").write_bytes(payload)
    env = _env(url, tmp_path)
    up = _run(
        ["upload", "--name", "big", "--path", "dist/", "--compression-level", "0", "--json"],
        env,
        work,
    )
    assert up.returncode == 0, up.stderr
    body = json.loads(up.stdout)
    assert body["name"] == "big"
    assert body["files"] == 1
    assert body["size"] > CHUNK
    assert "artifact-id=" in (tmp_path / "output").read_text(encoding="utf-8")
    assert len(fake.live_files()) == 3  # root, repo folder, one artifact
    chunk_puts = [r for r in fake.requests if r.method == "PUT" and "upload_id" in str(r.url)]
    assert len(chunk_puts) >= 2

    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    down = _run(["download", "--name", "big", "--path", "out"], _env(url, second), dl_dir)
    assert down.returncode == 0, down.stderr
    assert (dl_dir / "out" / "big.bin").read_bytes() == payload

    # Every access token was announced for masking, and no secret shows up anywhere else.
    refresh_token = json.loads(TOKEN_JSON)["refresh_token"]
    masked = _masks(up.stderr) + _masks(down.stderr)
    assert any(m.startswith("at-") for m in masked)
    surfaces = {
        "upload stdout": up.stdout,
        "upload stderr": _unmasked(up.stderr),
        "upload output file": _read(tmp_path / "output"),
        "upload summary file": _read(tmp_path / "summary"),
        "download stdout": down.stdout,
        "download stderr": _unmasked(down.stderr),
        "download output file": _read(second / "output"),
        "download summary file": _read(second / "summary"),
    }
    # Every access token the fake ever issued, not the bare prefix: `at-` may turn up in
    # benign text one day, a whole token may not.
    for secret in {refresh_token, *fake.issued_access_tokens, *masked}:
        for label, text in surfaces.items():
            assert secret not in text, f"{secret!r} leaked into the {label}"


def test_exit_codes_cross_the_process_boundary(base: tuple[FakeDrive, str], tmp_path: Path) -> None:
    _, url = base
    work = tmp_path / "work"
    work.mkdir()
    env = _env(url, tmp_path)
    _fails(_run(["download", "--name", "missing"], env, work), 4, "no artifact")
    _fails(
        _run(["upload", "--path", "nothing/*", "--if-no-files-found", "error"], env, work),
        7,
        "no files matched",
    )
    _fails(_run(["upload", "--path", ".", "--name", "x|y"], env, work), 2, "forbidden characters")
    no_token = {k: v for k, v in env.items() if k != "DRIVE_ARTIFACTS_TOKEN"}
    (work / "f").write_text("x")
    _fails(_run(["upload", "--path", "f"], no_token, work), 3, "DRIVE_ARTIFACTS_TOKEN")


def test_unreachable_api_is_a_drive_error_not_a_traceback(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "f").write_text("x")
    env = _env("http://127.0.0.1:9", tmp_path)  # nothing listens on port 9
    result = _run(["upload", "--path", "f"], env, work)
    # the token refresh is the first call, and it fails cleanly
    _fails(result, 3, "token endpoint")
    assert result.stdout == ""
