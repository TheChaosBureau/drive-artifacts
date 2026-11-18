# SPDX-License-Identifier: Apache-2.0
import io
import json
import logging
import os
import runpy
import sys
import threading
import webbrowser
import zipfile
from collections.abc import Callable, Iterator
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from drive_artifacts import __version__, _client, cli
from drive_artifacts.archive import safe_extract
from drive_artifacts.cli import SECRET_NAME, main
from drive_artifacts.drive import DriveClient, RetryPolicy
from drive_artifacts.model import ArtifactKey, ArtifactRecord
from tests.conftest import TOKEN_JSON
from tests.fake_drive import FakeDrive, Fault

ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "TheChaosBureau/drive-artifacts",
    "GITHUB_REPOSITORY_ID": "987",
    "GITHUB_RUN_ID": "12345",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_SHA": "a" * 40,
    "DRIVE_ARTIFACTS_CLIENT_ID": "cid",
    "DRIVE_ARTIFACTS_CLIENT_SECRET": "csec",
    "DRIVE_ARTIFACTS_TOKEN": TOKEN_JSON,
}


class Run:
    """One CLI invocation against the fake, with captured streams and GitHub files."""

    def __init__(self, fake: FakeDrive, tmp_path: Path, **env: str) -> None:
        self.fake = fake
        self.cwd = tmp_path / "work"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.out_file = tmp_path / "github_output"
        self.summary_file = tmp_path / "github_summary"
        self.env = {
            **ENV,
            "GITHUB_OUTPUT": str(self.out_file),
            "GITHUB_STEP_SUMMARY": str(self.summary_file),
            **env,
        }
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.handler = fake.handler  # a test may wrap it to act between two requests

    def __call__(
        self,
        *argv: str,
        open_browser: Callable[[str], None] | None = None,
        run_gh: Callable[[list[str], str], int] | None = None,
    ) -> int:
        return main(
            list(argv),
            env=self.env,
            stdout=self.stdout,
            stderr=self.stderr,
            cwd=self.cwd,
            transport=httpx.MockTransport(self.handler),
            open_browser=open_browser,
            run_gh=run_gh,
        )

    def outputs(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.out_file.exists():
            for line in self.out_file.read_text().splitlines():
                if "=" in line and not line.startswith("multi"):
                    k, v = line.split("=", 1)
                    result[k] = v
        return result

    def json(self) -> Any:
        return json.loads(self.stdout.getvalue())


@pytest.fixture
def run(fake: FakeDrive, tmp_path: Path) -> Run:
    r = Run(fake, tmp_path)
    (r.cwd / "dist").mkdir()
    (r.cwd / "dist" / "a.whl").write_bytes(b"wheel")
    (r.cwd / "dist" / "b.txt").write_bytes(b"notes")
    return r


def test_version_and_help(run: Run, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        run("--version")
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        run("--help")
    assert exc.value.code == 0


def test_upload_then_download_in_the_same_run(run: Run, fake: FakeDrive) -> None:
    assert (
        run("upload", "--name", "wheel", "--path", "dist/", "--retention-days", "7", "--json") == 0
    )
    out = run.outputs()
    assert out["artifact-id"] in fake.files
    assert out["artifact-digest"].startswith("sha256:")
    assert out["artifact-url"].startswith("https://drive.google.com/")
    assert "wheel" in run.summary_file.read_text()
    assert "expires" in run.summary_file.read_text()
    body = run.json()
    assert body["name"] == "wheel"
    assert body["files"] == 2
    dl = Run(fake, run.cwd.parent / "two")
    assert dl("download", "--name", "wheel", "--path", "out") == 0
    assert (dl.cwd / "out" / "a.whl").read_bytes() == b"wheel"
    assert (dl.cwd / "out" / "b.txt").read_bytes() == b"notes"
    assert dl.outputs()["download-path"] == str((dl.cwd / "out").resolve())
    assert dl.outputs()["artifact-digest"] == out["artifact-digest"]  # one artifact: its digest


def test_download_all_artifacts_of_a_run_goes_into_named_subdirectories(
    run: Run, fake: FakeDrive
) -> None:
    run("upload", "--name", "wheel", "--path", "dist/a.whl")
    run("upload", "--name", "notes", "--path", "dist/b.txt")
    dl = Run(fake, run.cwd.parent / "two")
    assert dl("download", "--path", "all") == 0
    assert (dl.cwd / "all" / "wheel" / "a.whl").exists()
    assert (dl.cwd / "all" / "notes" / "b.txt").exists()
    # two artifacts have no single digest: the output is absent, not an arbitrary one of them
    assert dl.outputs()["download-path"] == str((dl.cwd / "all").resolve())
    assert "artifact-digest" not in dl.outputs()


def test_download_latest_for_ref_and_by_run_id(run: Run, fake: FakeDrive) -> None:
    run("upload", "--name", "wheel", "--path", "dist/a.whl")
    later = Run(fake, run.cwd.parent / "later", GITHUB_RUN_ID="12346")
    (later.cwd / "dist").mkdir()
    (later.cwd / "dist" / "a.whl").write_bytes(b"newer")
    later("upload", "--name", "wheel", "--path", "dist/a.whl")
    dl = Run(fake, run.cwd.parent / "dl", GITHUB_RUN_ID="99999")
    assert dl("download", "--name", "wheel", "--ref", "refs/heads/main", "--path", "x") == 0
    assert (dl.cwd / "x" / "a.whl").read_bytes() == b"newer"
    dl2 = Run(fake, run.cwd.parent / "dl2", GITHUB_RUN_ID="99999")
    assert dl2("download", "--name", "wheel", "--run-id", "12345", "--path", "y") == 0
    assert (dl2.cwd / "y" / "a.whl").read_bytes() == b"wheel"
    outside = Run(fake, run.cwd.parent / "outside")
    for key in ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_REPOSITORY_ID", "GITHUB_REPOSITORY"):
        outside.env.pop(key)
    assert (
        outside(
            "download",
            "--name",
            "wheel",
            "--ref",
            "refs/heads/main",
            "--repo-id",
            "987",
            "--repo",
            "o/r",
            "--path",
            "z",
        )
        == 0
    )
    assert (outside.cwd / "z" / "a.whl").read_bytes() == b"newer"


def test_download_not_found_is_exit_4(run: Run) -> None:
    assert run("download", "--name", "nope") == 4
    assert "::error::" in run.stderr.getvalue()


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs a read-only directory the process cannot write",
)
def test_filesystem_errors_are_exit_1_not_integrity(run: Run, fake: FakeDrive) -> None:
    run("upload", "--name", "wheel", "--path", "dist/")
    locked = run.cwd / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert run("download", "--name", "wheel", "--path", "locked/out") == 1
        assert "filesystem error" in run.stderr.getvalue()
    finally:
        locked.chmod(0o700)


def test_if_no_files_found(run: Run) -> None:
    assert run("upload", "--path", "missing/**") == 0
    assert "::warning::" in run.stderr.getvalue()
    assert run("upload", "--path", "missing/**", "--if-no-files-found", "ignore") == 0
    assert run("upload", "--path", "missing/**", "--if-no-files-found", "error") == 7


def test_multiline_path_and_excludes(run: Run, fake: FakeDrive) -> None:
    assert run("upload", "--name", "w", "--path", "dist/**\n!dist/*.txt") == 0
    file = next(f for f in fake.live_files() if f.name.endswith("-w.zip"))
    with zipfile.ZipFile(io.BytesIO(file.content)) as zf:
        assert zf.namelist() == ["a.whl"]


def test_duplicate_upload_conflicts_and_overwrite_wins(run: Run) -> None:
    assert run("upload", "--name", "w", "--path", "dist/") == 0
    assert run("upload", "--name", "w", "--path", "dist/") == 5
    assert "already exists" in run.stderr.getvalue()
    assert run("upload", "--name", "w", "--path", "dist/", "--overwrite") == 0


def test_missing_token_is_exit_3_and_bad_name_is_exit_2(run: Run) -> None:
    run.env.pop("DRIVE_ARTIFACTS_TOKEN")
    assert run("upload", "--path", "dist/") == 3
    assert SECRET_NAME in run.stderr.getvalue()
    run.env["DRIVE_ARTIFACTS_TOKEN"] = TOKEN_JSON
    assert run("upload", "--name", "a:b", "--path", "dist/") == 2
    assert run("upload", "--path", "dist/", "--retention-days", "0") == 2


def test_revoked_grant_is_exit_3(run: Run, fake: FakeDrive) -> None:
    fake.refresh_tokens.clear()
    assert run("upload", "--path", "dist/") == 3
    assert "drive-artifacts login" in run.stderr.getvalue()


def test_access_tokens_are_masked(run: Run) -> None:
    run("upload", "--path", "dist/")
    assert "::add-mask::at-" in run.stderr.getvalue()


@pytest.fixture(autouse=True)
def _restore_logger_levels() -> Iterator[None]:
    """`main` sets logger levels process-wide; a test must not hand them to the next one."""
    names = ("drive_artifacts", "httpx", "httpcore")
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_verbose_never_logs_request_urls(run: Run, caplog: pytest.LogCaptureFixture) -> None:
    # Under pytest the root logger already has handlers, so `basicConfig` is a no-op: read the
    # records through caplog, and start httpx/httpcore at the noisiest level so that only
    # `main` pinning them can keep their request lines out.
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.DEBUG)
    assert run("--verbose", "upload", "--name", "w", "--path", "dist/") == 0
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    messages = [record.getMessage() for record in caplog.records]
    assert not any("upload_id" in m or "HTTP Request" in m for m in messages)
    assert "uploading w from ['dist/']" in messages  # --verbose does say something
    assert "upload_id=" not in run.stderr.getvalue()


def test_without_verbose_the_drive_artifacts_logger_stays_quiet(
    run: Run, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert run("upload", "--name", "w", "--path", "dist/") == 0
    assert [r for r in caplog.records if r.name == "drive_artifacts"] == []


def test_verbose_says_one_thing_when_each_command_starts(
    run: Run, fake: FakeDrive, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    steps = [
        ("upload", "--name", "w", "--path", "dist/"),
        ("download", "--name", "w", "--path", "out"),
        ("list",),
        ("prune", "--dry-run"),
        ("whoami",),
        ("login", "--print"),
        ("logout",),
    ]
    for argv in steps:
        caplog.clear()
        assert run("--verbose", *argv, open_browser=_browser(fake)) == 0, argv
        ours = [r.getMessage() for r in caplog.records if r.name == "drive_artifacts"]
        assert len(ours) == 1, (argv, ours)
    run.env.pop("DRIVE_ARTIFACTS_TOKEN")
    caplog.clear()
    assert run("--verbose", "whoami") == 0  # no token: still says what it does
    assert [r.getMessage() for r in caplog.records if r.name == "drive_artifacts"] == [
        "no token set; skipping the root folder"
    ]


def test_list_and_prune(run: Run, fake: FakeDrive) -> None:
    run("upload", "--name", "w", "--path", "dist/", "--retention-days", "1")
    assert run("list", "--json") == 0
    assert [r["name"] for r in run.json()] == ["w"]
    run.stdout.truncate(0)
    run.stdout.seek(0)
    assert run("list", "--all", "--json") == 0
    assert len(run.json()) == 1
    outside = Run(fake, run.cwd.parent / "outside")
    for key in ("GITHUB_ACTIONS", "GITHUB_RUN_ID"):
        outside.env.pop(key)
    assert outside("list", "--all", "--json") == 0
    assert len(outside.json()) == 1
    assert outside("list") == 2  # no run id anywhere
    for f in fake.live_files():
        if f.app_properties.get("da.kind") == "artifact":
            f.app_properties["da.expires"] = "2000-01-01"
    assert run("prune", "--dry-run") == 0
    assert "1 expired" in run.stderr.getvalue()
    assert all(not f.trashed for f in fake.files.values())
    assert run("prune") == 0
    assert any(f.trashed for f in fake.files.values())


def test_upload_prunes_expired_artifacts_unless_told_not_to(run: Run, fake: FakeDrive) -> None:
    run("upload", "--name", "old", "--path", "dist/")
    for f in fake.live_files():
        if f.app_properties.get("da.kind") == "artifact":
            f.app_properties["da.expires"] = "2000-01-01"
    run("upload", "--name", "new", "--path", "dist/", "--no-prune")
    assert not any(f.trashed for f in fake.files.values())
    run("upload", "--name", "newer", "--path", "dist/")
    assert sum(1 for f in fake.files.values() if f.trashed) == 1


def test_whoami(run: Run) -> None:
    assert run("whoami", "--json") == 0
    body = run.json()
    assert body["client_id"] == "cid"
    assert body["token"] is True
    assert body["root"] is None
    run("upload", "--path", "dist/")
    run.stdout.truncate(0)
    run.stdout.seek(0)
    run("whoami", "--json")
    assert run.json()["root"]["name"] == "GitHub Artifacts"


def test_logout_revokes(run: Run, fake: FakeDrive) -> None:
    assert run("logout") == 0
    assert "rt-1" in fake.revoked
    assert "delete" in run.stderr.getvalue()
    assert "same Google account" in run.stderr.getvalue()


def _browser(fake: FakeDrive) -> Callable[[str], None]:
    def open_browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        params = {"code": fake.issue_auth_code(q["code_challenge"]), "state": q["state"]}
        threading.Thread(
            target=lambda: httpx.get(q["redirect_uri"], params=params, timeout=5), daemon=True
        ).start()

    return open_browser


GhCalls = list[tuple[list[str], str]]


def _gh(calls: GhCalls, *, auth: int = 0, secret: int = 0) -> Callable[[list[str], str], int]:
    """A stand-in for gh: `gh auth status` exits `auth`, `gh secret set` exits `secret`."""

    def gh(args: list[str], stdin: str) -> int:
        calls.append((args, stdin))
        return auth if args[:2] == ["auth", "status"] else secret

    return gh


def test_login_print_and_gh(run: Run, fake: FakeDrive) -> None:
    unused: GhCalls = []
    assert run("login", "--print", open_browser=_browser(fake), run_gh=_gh(unused, auth=1)) == 0
    assert unused == []  # --print never asks gh anything
    token = json.loads(run.stdout.getvalue())
    assert token["client_id"] == "cid"
    assert token["refresh_token"] in fake.refresh_tokens
    calls: GhCalls = []
    assert run("login", "--repo", "o/r", open_browser=_browser(fake), run_gh=_gh(calls)) == 0
    assert [args for args, _ in calls] == [
        ["auth", "status"],
        ["secret", "set", SECRET_NAME, "--repo", "o/r"],
    ]
    assert calls[0][1] == ""
    assert json.loads(calls[1][1])["refresh_token"] in fake.refresh_tokens
    assert run("login", "--org", "acme", open_browser=_browser(fake), run_gh=_gh([], secret=1)) == 2
    assert "gh secret set" in run.stderr.getvalue()


def test_login_checks_gh_before_the_browser_opens(run: Run, fake: FakeDrive) -> None:
    calls: GhCalls = []
    opened: list[str] = []
    assert (
        run(
            "login",
            "--repo",
            "o/r",
            "--timeout",
            "1",
            open_browser=opened.append,
            run_gh=_gh(calls, auth=1),
        )
        == 2
    )
    err = run.stderr.getvalue()
    assert "gh is not authenticated" in err
    assert "gh auth login" in err
    assert "--print" in err
    assert [args for args, _ in calls] == [["auth", "status"]]  # and never `secret set`
    assert opened == []  # no consent flow started
    assert fake.auth_codes == {}
    assert fake.requests == []  # not even the token endpoint
    assert fake.refresh_tokens == {"rt-1"}  # no grant minted, so none to revoke
    assert fake.revoked == []


def test_login_refuses_the_unset_client(
    run: Run, fake: FakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fork that blanks the shipped client gets a clear refusal, not a Google error page.
    monkeypatch.setattr(_client, "CLIENT_ID", "unset.apps.googleusercontent.com")
    run.env.pop("DRIVE_ARTIFACTS_CLIENT_ID")
    run.env.pop("DRIVE_ARTIFACTS_CLIENT_SECRET")
    assert run("login", "--print", open_browser=_browser(fake)) == 2


def test_login_needs_a_target(run: Run) -> None:
    with pytest.raises(SystemExit) as exc:
        run("login")
    assert exc.value.code == 2


# ----- beyond the brief: the branches the happy paths do not reach -----------------------------


def test_a_filesystem_error_is_exit_1_with_an_annotation(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "build_zip", full_disk)
    assert run("upload", "--path", "dist/") == 1
    assert "filesystem error: " in run.stderr.getvalue()
    assert "No space left on device" in run.stderr.getvalue()


def test_ctrl_c_is_exit_130(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "resolve_paths", interrupt)
    assert run("upload", "--path", "dist/") == 130
    assert "interrupted" in run.stderr.getvalue()


def test_download_by_ref_or_commit_needs_a_name(run: Run) -> None:
    assert run("download", "--ref", "refs/heads/main") == 2
    assert "need --name" in run.stderr.getvalue()
    assert run("download", "--commit", "a" * 40) == 2


def test_download_latest_for_a_commit(run: Run, fake: FakeDrive) -> None:
    run("upload", "--name", "wheel", "--path", "dist/a.whl")
    dl = Run(fake, run.cwd.parent / "dl", GITHUB_RUN_ID="99999")
    assert dl("download", "--name", "wheel", "--commit", "a" * 40, "--path", "c") == 0
    assert (dl.cwd / "c" / "a.whl").read_bytes() == b"wheel"
    assert dl("download", "--name", "wheel", "--commit", "b" * 40, "--path", "d") == 4


def test_list_ref_asks_about_the_repository_not_one_run(run: Run, fake: FakeDrive) -> None:
    run("upload", "--name", "on-main", "--path", "dist/a.whl")
    dev = Run(fake, run.cwd.parent / "dev", GITHUB_RUN_ID="12346", GITHUB_REF="refs/heads/dev")
    (dev.cwd / "dist").mkdir()
    (dev.cwd / "dist" / "a.whl").write_bytes(b"dev")
    dev("upload", "--name", "on-dev", "--path", "dist/a.whl")
    assert run("list", "--ref", "refs/heads/dev", "--json") == 0
    assert [r["name"] for r in run.json()] == ["on-dev"]
    for selector in ("--ref=refs/heads/dev", "--all"):
        run.stderr.truncate(0)
        run.stderr.seek(0)
        assert run("list", selector, "--run-id", "12345") == 2
        assert "--run-id cannot be combined with --all or --ref" in run.stderr.getvalue()


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("list", "--commit", "x"), id="list-has-no-commit"),
        pytest.param(("list", "--run-attempt", "2"), id="list-has-no-attempt"),
        pytest.param(("download", "--run-attempt", "2"), id="download-takes-the-newest-attempt"),
        pytest.param(("prune", "--run-id", "1"), id="prune-is-per-repository"),
        pytest.param(("prune", "--ref", "refs/heads/main"), id="prune-has-no-ref"),
        pytest.param(("prune", "--run-attempt", "1"), id="prune-has-no-attempt"),
    ],
)
def test_flags_a_command_would_ignore_are_refused(
    run: Run, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as exc:
        run(*argv)
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_list_prints_a_tab_separated_table(run: Run) -> None:
    run("upload", "--name", "w", "--path", "dist/", "--retention-days", "3")
    assert run("list") == 0
    (line,) = run.stdout.getvalue().splitlines()
    run_id, attempt, name, size, expires, url = line.split("\t")
    assert (run_id, attempt, name) == ("12345", "1", "w")
    assert int(size) > 0
    assert len(expires) == 10
    assert url.startswith("https://drive.google.com/")


def test_the_flags_that_remain_still_work_outside_actions(run: Run, fake: FakeDrive) -> None:
    assert run("upload", "--name", "w", "--path", "dist/a.whl", "--run-attempt", "2") == 0
    outside = Run(fake, run.cwd.parent / "outside")
    for key in ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_REPOSITORY_ID", "GITHUB_REPOSITORY"):
        outside.env.pop(key)
    repo = ("--repo-id", "987", "--repo", "o/r")
    assert outside("list", *repo, "--run-id", "12345", "--json") == 0
    assert [(r["name"], r["run_attempt"]) for r in outside.json()] == [("w", "2")]
    assert outside("prune", *repo, "--dry-run") == 0
    assert outside("download", *repo, "--run-id", "12345", "--path", "got") == 0
    assert (outside.cwd / "got" / "w" / "a.whl").read_bytes() == b"wheel"


def test_whoami_prints_plain_text(run: Run) -> None:
    assert run("whoami") == 0
    assert run.stdout.getvalue() == "client: cid\ntoken: set\nroot folder: none yet\n"
    run("upload", "--path", "dist/")
    run.stdout.truncate(0)
    run.stdout.seek(0)
    assert run("whoami") == 0
    root = next(f for f in run.fake.live_files() if f.app_properties.get("da.kind") == "root")
    assert run.stdout.getvalue() == (
        "client: cid\ntoken: set\n"
        f"root folder: GitHub Artifacts ({root.id}) https://drive.google.com/file/d/{root.id}/view\n"
    )
    run.env.pop("DRIVE_ARTIFACTS_TOKEN")
    run.stdout.truncate(0)
    run.stdout.seek(0)
    assert run("whoami") == 0
    assert "token: not set" in run.stdout.getvalue()


def test_logout_without_a_token_is_exit_3(run: Run, fake: FakeDrive) -> None:
    run.env.pop("DRIVE_ARTIFACTS_TOKEN")
    assert run("logout") == 3
    assert not fake.revoked


def test_the_refresh_token_never_reaches_either_stream(run: Run, fake: FakeDrive) -> None:
    run("upload", "--name", "w", "--path", "dist/")
    run("list", "--all", "--json")
    run("whoami", "--json")
    run("prune", "--dry-run", "--json")
    dl = Run(fake, run.cwd.parent / "dl")
    dl("download", "--name", "w", "--json")
    run("logout")
    for r in (run, dl):
        seen = r.stdout.getvalue() + r.stderr.getvalue()
        for written in (r.summary_file, r.out_file):
            if written.exists():
                seen += written.read_text()
        assert seen
        assert "rt-1" not in seen


def test_login_to_gh_keeps_the_secret_off_both_streams(run: Run, fake: FakeDrive) -> None:
    calls: GhCalls = []
    assert run("login", "--repo", "o/r", open_browser=_browser(fake), run_gh=_gh(calls)) == 0
    args, stdin = calls[-1]
    assert args[:2] == ["secret", "set"]
    refresh = json.loads(stdin)["refresh_token"]
    assert refresh not in run.stdout.getvalue() + run.stderr.getvalue()
    assert all(refresh not in arg for args, _ in calls for arg in args)


def test_the_default_browser_prints_the_url_then_opens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def record(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    err = io.StringIO()
    cli._default_open_browser(err)("https://example.test/auth")
    assert "https://example.test/auth" in err.getvalue()
    assert opened == ["https://example.test/auth"]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in for gh is a POSIX shell script")
def test_the_default_gh_runner_passes_the_secret_on_stdin_only(
    run: Run, fake: FakeDrive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo "$1 $2" >> "$GH_LOG"\n'
        'if [ "$1" = auth ]; then exit "$GH_AUTH_EXIT"; fi\n'
        'printf "%s\\n" "$@" > "$GH_ARGV"\ncat > "$GH_STDIN"\nexit "$GH_EXIT"\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")  # the stub wins
    monkeypatch.setenv("GH_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("GH_ARGV", str(tmp_path / "argv"))
    monkeypatch.setenv("GH_STDIN", str(tmp_path / "stdin"))
    monkeypatch.setenv("GH_AUTH_EXIT", "0")
    monkeypatch.setenv("GH_EXIT", "0")
    assert run("login", "--repo", "o/r", open_browser=_browser(fake)) == 0
    assert (tmp_path / "log").read_text().splitlines() == ["auth status", "secret set"]
    assert (tmp_path / "argv").read_text().split() == [
        "secret",
        "set",
        SECRET_NAME,
        "--repo",
        "o/r",
    ]
    secret = json.loads((tmp_path / "stdin").read_text())
    assert secret["refresh_token"] in fake.refresh_tokens
    assert secret["refresh_token"] not in (tmp_path / "argv").read_text()
    monkeypatch.setenv("GH_EXIT", "1")
    assert run("login", "--org", "acme", open_browser=_browser(fake)) == 2
    assert "exit 1" in run.stderr.getvalue()
    unstored = json.loads((tmp_path / "stdin").read_text())["refresh_token"]
    assert unstored != secret["refresh_token"]
    assert fake.revoked == []  # a failed login revokes nothing: see cmd_login
    assert secret["refresh_token"] in fake.refresh_tokens
    # gh present but logged out: refused before the browser, so no grant is minted
    monkeypatch.setenv("GH_AUTH_EXIT", "1")
    tokens_before = set(fake.refresh_tokens)
    opened: list[str] = []
    assert run("login", "--org", "acme", "--timeout", "1", open_browser=opened.append) == 2
    assert "gh is not authenticated" in run.stderr.getvalue()
    assert opened == []
    assert fake.refresh_tokens == tokens_before
    assert fake.revoked == []
    assert (tmp_path / "log").read_text().splitlines()[-2:] == ["secret set", "auth status"]


def test_login_without_gh_on_the_path_points_at_print(
    run: Run, fake: FakeDrive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    opened: list[str] = []
    assert run("login", "--repo", "o/r", "--timeout", "1", open_browser=opened.append) == 2
    assert "gh is not on PATH" in run.stderr.getvalue()
    assert "--print" in run.stderr.getvalue()
    assert opened == []  # refused before the browser opened: no grant was minted
    assert fake.requests == []
    assert fake.revoked == []


def test_main_reads_the_process_environment_and_streams_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in [k for k in os.environ if k.startswith(("GITHUB_", "DRIVE_ARTIFACTS_"))]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("DRIVE_ARTIFACTS_CLIENT_ID", "from-the-process")
    monkeypatch.chdir(tmp_path)
    assert main(["whoami", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body == {"client_id": "from-the-process", "token": False, "root": None}


def test_python_dash_m_exits_with_the_exit_code_of_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["drive-artifacts", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("drive_artifacts", run_name="__main__")
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


# ----- fix round 1: names from Drive are not paths, and the smaller review findings ------------


def test_dot_names_are_refused_at_upload(run: Run, fake: FakeDrive) -> None:
    for name in (".", "..", " .. "):
        assert run("upload", "--name", name, "--path", "dist/") == 2
    assert "must not be" in run.stderr.getvalue()
    assert not fake.files  # refused before anything reached Drive


@pytest.mark.parametrize("planted", ["..", ".", "a/b", "../x", "x/../..", " x", "x ", "ev\x00il"])
def test_a_planted_artifact_name_is_skipped_and_nothing_leaves_path(
    run: Run, fake: FakeDrive, planted: str
) -> None:
    run("upload", "--name", "good", "--path", "dist/a.whl")
    run("upload", "--name", "evil", "--path", "dist/b.txt")
    (evil,) = (f for f in fake.live_files() if f.app_properties.get("da.name") == "evil")
    evil.app_properties["da.name"] = planted
    assert run("list", "--json") == 0
    assert [r["name"] for r in run.json()] == ["good"]
    dl = Run(fake, run.cwd.parent / "two")
    assert dl("download", "--path", "all") == 0
    assert sorted(p.name for p in dl.cwd.iterdir()) == ["all"]  # nothing beside --path
    assert [p.name for p in (dl.cwd / "all").iterdir()] == ["good"]
    assert (dl.cwd / "all" / "good" / "a.whl").read_bytes() == b"wheel"
    assert dl("download", "--name", planted, "--path", "named") == 4
    assert not (dl.cwd / "named").exists()


def test_a_planted_absolute_name_is_skipped_too(run: Run, fake: FakeDrive, tmp_path: Path) -> None:
    run("upload", "--name", "good", "--path", "dist/a.whl")
    run("upload", "--name", "evil", "--path", "dist/b.txt")
    (evil,) = (f for f in fake.live_files() if f.app_properties.get("da.name") == "evil")
    elsewhere = tmp_path / "elsewhere"
    evil.app_properties["da.name"] = str(elsewhere)
    dl = Run(fake, run.cwd.parent / "two")
    assert dl("download", "--path", "all") == 0
    assert not elsewhere.exists()
    assert [p.name for p in (dl.cwd / "all").iterdir()] == ["good"]


def _record_named(name: str) -> ArtifactRecord:
    return ArtifactRecord(
        key=ArtifactKey("987", "12345", "1", name),  # built by hand: from_drive would refuse it
        file_id="never-fetched",
        sha256="0" * 64,
        size=1,
        ref="",
        commit="",
        expires=date(2099, 1, 1),
        web_view_link="",
        modified_time="",
    )


def test_download_all_itself_refuses_a_name_that_would_leave_the_path(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second wall: even if a record with such a name got past `from_drive`."""
    monkeypatch.setattr(cli, "_select_for_download", lambda _e, _store: [_record_named("..")])
    dl = Run(run.fake, run.cwd.parent / "two")
    assert dl("download", "--path", "all") == 6
    assert "outside" in dl.stderr.getvalue()
    assert list(dl.cwd.iterdir()) == []  # not even `all/`


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_download_all_refuses_a_subdirectory_that_is_a_link_out_of_the_path(
    run: Run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "_select_for_download", lambda _e, _store: [_record_named("linked")])
    dl = Run(run.fake, run.cwd.parent / "two")
    (dl.cwd / "all").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (dl.cwd / "all" / "linked").symlink_to(elsewhere, target_is_directory=True)
    assert dl("download", "--path", "all") == 6
    assert list(elsewhere.iterdir()) == []


def test_the_temporary_zip_is_named_by_the_drive_id_not_the_artifact_name(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run("upload", "--name", "w", "--path", "dist/")
    (file,) = (f for f in run.fake.live_files() if f.app_properties.get("da.kind") == "artifact")
    seen: list[Path] = []

    def spy(zip_path: Path, dest: Path) -> list[Path]:
        seen.append(zip_path)
        return safe_extract(zip_path, dest)

    monkeypatch.setattr(cli, "safe_extract", spy)
    dl = Run(run.fake, run.cwd.parent / "two")
    assert dl("download", "--name", "w") == 0
    assert [p.name for p in seen] == [f"{file.id}.zip"]


def _fast_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real client, with retries that exhaust at once instead of sleeping for minutes."""
    monkeypatch.setattr(
        cli,
        "DriveClient",
        partial(
            DriveClient, policy=RetryPolicy(max_attempts=3, deadline_s=30), sleep=lambda _s: None
        ),
    )


def test_a_failed_prune_after_a_stored_upload_is_a_warning_not_a_failure(
    run: Run, fake: FakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_drive(monkeypatch)
    handle = fake.handler

    def break_listing_once_uploaded(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        if request.method == "PUT":  # the artifact has landed; the prune query comes next
            fake.faults.append(Fault(status=503, method="GET", count=99))
        return response

    run.handler = break_listing_once_uploaded
    assert run("upload", "--name", "w", "--path", "dist/", "--json") == 0
    assert "::warning::prune failed: " in run.stderr.getvalue()
    assert "::error::" not in run.stderr.getvalue()
    assert run.json()["name"] == "w"  # the document is still printed
    assert run.outputs()["artifact-id"] in fake.files
    assert any(f.app_properties.get("da.kind") == "artifact" for f in fake.live_files())


def test_a_failed_gh_revokes_nothing(run: Run, fake: FakeDrive) -> None:
    """Google may answer a revoke by ending the whole sign-in for the account and client, which
    would kill the token every other repository already stores. So the unstored grant stays."""
    calls: GhCalls = []
    assert (
        run("login", "--org", "acme", open_browser=_browser(fake), run_gh=_gh(calls, secret=1)) == 2
    )
    minted = json.loads(calls[-1][1])["refresh_token"]
    assert fake.revoked == []
    assert not any(r.url.path.endswith("/revoke") for r in fake.requests)
    assert minted in fake.refresh_tokens
    assert "rt-1" in fake.refresh_tokens  # the grant already in use is untouched
    err = run.stderr.getvalue()
    assert "gh secret set" in err
    assert "--print" in err
    assert "was not stored" in err
    assert minted not in run.stdout.getvalue() + err
    assert run("whoami") == 0  # the stored token still works


def test_download_all_with_nothing_stored_succeeds_with_a_warning(run: Run) -> None:
    """`actions/download-artifact` without a name succeeds with "0 artifact(s)" on a run that
    stored nothing; only a download that names an artifact fails when it is missing."""
    assert run("download", "--path", "all", "--json") == 0
    err = run.stderr.getvalue()
    assert "::warning::no artifacts found for run 12345" in err
    assert "::error::" not in err
    assert run.outputs()["download-path"] == str((run.cwd / "all").resolve())
    assert "artifact-digest" not in run.outputs()
    assert run.json() == []
    assert run("download", "--name", "nope", "--path", "all") == 4


def test_download_path_expands_a_leading_tilde(
    run: Run, fake: FakeDrive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    run("upload", "--name", "wheel", "--path", "dist/")
    assert run("download", "--name", "wheel", "--path", "~/out") == 0
    assert (home / "out" / "a.whl").read_bytes() == b"wheel"
    assert not (run.cwd / "~").exists()
    assert run.outputs()["download-path"] == str((home / "out").resolve())


def test_a_closed_stdout_pipe_ends_quietly(run: Run) -> None:
    """`drive-artifacts list | head -1`: the reader left; that is not a filesystem error."""

    class ClosedPipe(io.StringIO):
        def write(self, _text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    run("upload", "--name", "w", "--path", "dist/")
    before = run.stderr.getvalue()
    run.stdout = ClosedPipe()
    assert run("list") == 141
    said = run.stderr.getvalue()[len(before) :]
    assert "::error::" not in said  # no annotation, and no traceback either
    assert "Traceback" not in said
    assert "filesystem error" not in said


def test_gh_that_cannot_be_executed_points_at_print(
    run: Run, fake: FakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("shutil.which", lambda _name: "/opt/bin/gh")
    monkeypatch.setattr("subprocess.run", denied)
    opened: list[str] = []
    assert run("login", "--repo", "o/r", "--timeout", "1", open_browser=opened.append) == 2
    err = run.stderr.getvalue()
    assert "could not run gh" in err
    assert "--print" in err
    assert "filesystem error" not in err
    assert opened == []
