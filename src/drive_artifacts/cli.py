# SPDX-License-Identifier: Apache-2.0
"""The command line. Every subcommand is a thin function over the store; exit codes live here."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx

from drive_artifacts import __version__, _client
from drive_artifacts.archive import build_zip, resolve_paths, safe_extract
from drive_artifacts.drive import DriveClient
from drive_artifacts.errors import (
    AuthError,
    DriveArtifactsError,
    IntegrityError,
    NoFilesError,
    NotFoundError,
    UsageError,
)
from drive_artifacts.github import Actions, read_run_context
from drive_artifacts.model import ArtifactRecord, RunContext, validate_artifact_name
from drive_artifacts.oauth import RefreshingTokenSource, StoredToken, loopback_login, revoke
from drive_artifacts.settings import Settings
from drive_artifacts.store import ArtifactStore, validate_retention_days

SECRET_NAME = "DRIVE_ARTIFACTS_TOKEN"  # noqa: S105 - the name of the secret, not its value
log = logging.getLogger("drive_artifacts")


@dataclass(slots=True)
class Env:
    """Everything one command needs: arguments, environment, settings, streams, and the browser
    and gh runners (injected, so tests can replace them)."""

    args: argparse.Namespace
    env: Mapping[str, str]
    settings: Settings
    actions: Actions
    stdout: TextIO
    stderr: TextIO
    cwd: Path
    http: httpx.Client
    open_browser: Callable[[str], None]
    run_gh: Callable[[list[str], str], int]


def _context_flags(
    parser: argparse.ArgumentParser, *, with_run: bool = True, with_attempt: bool = True
) -> None:
    """The run-context flags a command actually reads; a command never accepts one it ignores."""
    group = parser.add_argument_group("run context (read from GITHUB_* when omitted)")
    group.add_argument("--repo-id", help="GitHub numeric repository id")
    group.add_argument("--repo", help="owner/name, for display")
    if with_attempt:
        group.add_argument("--run-attempt")
    if with_run:  # `download` and `list` own their selectors, with a different meaning
        group.add_argument("--run-id")
        group.add_argument("--ref")
        group.add_argument("--commit")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drive-artifacts",
        description="GitHub Actions artifacts, stored in your own Google Drive.",
    )
    p.add_argument("--version", action="version", version=f"drive-artifacts {__version__}")
    p.add_argument("--verbose", action="store_true", help="log what the command does to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="zip files and store them as one artifact")
    up.add_argument("--name", default="artifact")
    up.add_argument(
        "--path",
        action="append",
        required=True,
        help="glob, directory or file; repeat or separate with newlines; `!` excludes",
    )
    up.add_argument("--retention-days", type=int, default=90)
    up.add_argument("--if-no-files-found", choices=["warn", "error", "ignore"], default="warn")
    up.add_argument("--compression-level", type=int, default=6, choices=range(10), metavar="0..9")
    up.add_argument("--overwrite", action="store_true")
    up.add_argument("--include-hidden-files", action="store_true")
    up.add_argument(
        "--prune",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="trash this repository's expired artifacts after the upload",
    )
    up.add_argument("--json", action="store_true")
    _context_flags(up)

    down = sub.add_parser("download", help="download and extract artifacts")
    down.add_argument(
        "--name", help="omit to download every artifact of the run, each into its own subdirectory"
    )
    down.add_argument("--path", default=".")
    which = down.add_mutually_exclusive_group()
    which.add_argument(
        "--run-id",
        dest="which_run_id",
        help="another run (default: this run); the newest attempt of each artifact wins",
    )
    which.add_argument(
        "--ref", dest="which_ref", help="latest artifact for this ref, e.g. refs/heads/main"
    )
    which.add_argument("--commit", dest="which_commit", help="latest artifact for this commit")
    down.add_argument("--json", action="store_true")
    _context_flags(down, with_run=False, with_attempt=False)

    ls = sub.add_parser("list", help="list artifacts")
    ls.add_argument(
        "--all", action="store_true", help="every artifact of the repository, newest first"
    )
    ls.add_argument("--run-id", help="list another run (default: this run)")
    ls.add_argument("--ref", help="list every artifact stored for this ref, newest first")
    ls.add_argument("--json", action="store_true")
    _context_flags(ls, with_run=False, with_attempt=False)

    pr = sub.add_parser("prune", help="trash expired artifacts of this repository")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--json", action="store_true")
    _context_flags(pr, with_run=False, with_attempt=False)

    lo = sub.add_parser(
        "login",
        help="authorise this app on your Google account and store the token as a GitHub secret",
    )
    target = lo.add_mutually_exclusive_group(required=True)
    target.add_argument("--repo", help="owner/name: store the secret on this repository with gh")
    target.add_argument("--org", help="store the secret on this organisation with gh")
    target.add_argument(
        "--print", action="store_true", help="print the secret value instead of storing it"
    )
    lo.add_argument("--timeout", type=float, default=300.0)

    sub.add_parser("logout", help="revoke the token in DRIVE_ARTIFACTS_TOKEN")

    who = sub.add_parser(
        "whoami", help="show the configured client, token presence and the root folder"
    )
    who.add_argument("--json", action="store_true")
    return p


# ----- wiring ------------------------------------------------------------------
def _stored(e: Env) -> StoredToken:
    if e.settings.token_text is None:
        raise AuthError(
            f"{SECRET_NAME} is not set; run `drive-artifacts login` and add the secret to the repository"
        )
    return StoredToken.parse(e.settings.token_text)


def _store(e: Env) -> ArtifactStore:
    tokens = RefreshingTokenSource(e.http, e.settings, _stored(e), on_new_token=e.actions.mask)
    return ArtifactStore(DriveClient(e.http, tokens, e.settings), root_name=e.settings.root_name)


def _ctx(e: Env, *, require_run: bool = True) -> RunContext:
    a = e.args
    return read_run_context(
        e.env,
        repo_id=a.repo_id,
        repo=a.repo,
        run_id=getattr(a, "run_id", None),
        run_attempt=getattr(a, "run_attempt", None),
        ref=getattr(a, "ref", None),
        commit=getattr(a, "commit", None),
        require_run=require_run,
    )


def _emit(e: Env, payload: object) -> None:
    if getattr(e.args, "json", False):
        print(json.dumps(payload, indent=2), file=e.stdout)


# ----- commands ------------------------------------------------------------------
def cmd_upload(e: Env) -> int:
    """`upload`: resolve the paths, build one zip, store it, write the outputs, then prune."""
    a = e.args
    ctx = _ctx(e)
    name = validate_artifact_name(a.name)
    validate_retention_days(a.retention_days)  # before anything is zipped or sent
    patterns = [line for chunk in a.path for line in chunk.splitlines()]
    log.info("uploading %s from %s", name, patterns)
    resolved = resolve_paths(patterns, e.cwd, include_hidden=a.include_hidden_files)
    if not resolved.files and not resolved.empty_dirs:
        message = f"no files matched {' | '.join(p for p in patterns if p.strip())!r}"
        if a.if_no_files_found == "error":
            raise NoFilesError(message)
        if a.if_no_files_found == "warn":
            e.actions.warning(message)
        return 0
    store = _store(e)
    with tempfile.TemporaryDirectory(prefix="drive-artifacts-") as td:
        built = build_zip(resolved, Path(td) / f"{name}.zip", compression_level=a.compression_level)
        record = store.put(
            ctx,
            name,
            built.path,
            sha256=built.sha256,
            size=built.size,
            retention_days=a.retention_days,
            overwrite=a.overwrite,
        )
    e.actions.output("artifact-id", record.file_id)
    e.actions.output("artifact-url", record.web_view_link)
    e.actions.output("artifact-digest", f"sha256:{record.sha256}")
    e.actions.output("size", str(record.size))
    e.actions.summary(
        f"- **{name}**: {built.file_count} file(s), {record.size:,} bytes, stored in Drive as "
        f"[{record.key.filename}]({record.web_view_link}); expires {record.expires.isoformat()}"
    )
    e.actions.notice(
        f"uploaded {name}: {built.file_count} file(s), {record.size} bytes, "
        f"expires {record.expires.isoformat()}"
    )
    if a.prune:
        try:
            pruned = store.prune(ctx.repo.id)
        except DriveArtifactsError as exc:
            # The artifact is stored and its outputs are written: housekeeping must not fail the step.
            e.actions.warning(f"prune failed: {exc}")
        else:
            if pruned:
                e.actions.notice(f"pruned {len(pruned)} expired artifact(s)")
    _emit(e, {**record.to_json(), "files": built.file_count})
    return 0


def _select_for_download(e: Env, store: ArtifactStore) -> list[ArtifactRecord]:
    a = e.args
    ctx = _ctx(e, require_run=not (a.which_ref or a.which_commit or a.which_run_id))
    if a.which_ref or a.which_commit:
        if not a.name:
            raise UsageError("--ref and --commit downloads need --name")
        record = store.latest(ctx.repo.id, a.name, ref=a.which_ref, commit=a.which_commit)
        return [record] if record else []
    run_id = a.which_run_id or ctx.run_id
    records = store.list_run(ctx.repo.id, run_id)
    if a.name:
        records = [r for r in records if r.key.name == a.name]
    newest: dict[str, ArtifactRecord] = {}
    # list_run sorts by (name, created_time): the last one per name is the newest attempt.
    for record in records:
        newest[record.key.name] = record
    return list(newest.values())


def _target_dir(dest: Path, record: ArtifactRecord, *, named: bool) -> Path:
    """Where `record` extracts. The name came from Drive: it must not lead out of `dest`."""
    if named:
        return dest
    target = (dest / record.key.name).resolve()
    if dest not in target.parents:  # strictly inside: `.` would merge it into --path itself
        raise IntegrityError(
            f"artifact name {record.key.name!r} would extract outside {dest}; refusing to download it"
        )
    return target


def cmd_download(e: Env) -> int:
    """`download`: fetch one artifact, or every artifact of a run, verify each digest and
    extract."""
    a = e.args
    store = _store(e)
    records = _select_for_download(e, store)
    if not records and a.name:
        raise NotFoundError(f"no artifact matches {a.name} for the requested run, ref or commit")
    if not records:
        # As actions/download-artifact does without a name: a run that stored nothing is not
        # an error, there is just nothing to extract.
        run_id = a.which_run_id or _ctx(e, require_run=False).run_id
        e.actions.warning(f"no artifacts found for run {run_id}; nothing was downloaded")
    log.info("downloading %d artifact(s) into %s", len(records), a.path)
    dest = (e.cwd / Path(a.path).expanduser()).resolve()
    extracted: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="drive-artifacts-") as td:
        for record in records:
            target = _target_dir(dest, record, named=bool(a.name))
            # Named by Drive id (a plain token), never by artifact name (arbitrary text).
            zip_path = Path(td) / f"{record.file_id}.zip"
            store.fetch(record, zip_path)
            files = safe_extract(zip_path, target)
            e.actions.notice(f"downloaded {record.key.name}: {len(files)} file(s) into {target}")
            extracted.append({**record.to_json(), "path": str(target), "files": len(files)})
    e.actions.output("download-path", str(dest))
    if len(records) == 1:  # one digest names one artifact; for several there is no single answer
        e.actions.output("artifact-digest", f"sha256:{records[0].sha256}")
    _emit(e, extracted)
    return 0


def cmd_list(e: Env) -> int:
    """`list`: the artifacts of one run, or of the whole repository with `--all` or `--ref`."""
    a = e.args
    repo_wide = a.all or bool(a.ref)  # an explicit --ref asks about the repository, not one run
    if repo_wide and a.run_id:
        raise UsageError("--run-id cannot be combined with --all or --ref")
    store = _store(e)
    ctx = _ctx(e, require_run=not repo_wide)
    log.info(
        "listing %s of %s", "every artifact" if repo_wide else f"run {ctx.run_id}", ctx.repo.slug
    )
    records = (
        store.list_repo(ctx.repo.id, ref=a.ref)
        if repo_wide
        else store.list_run(ctx.repo.id, ctx.run_id)
    )
    if a.json:
        _emit(e, [r.to_json() for r in records])
    else:
        for r in records:
            print(
                f"{r.key.run_id}\t{r.key.run_attempt}\t{r.key.name}\t{r.size}\t"
                f"{r.expires.isoformat()}\t{r.web_view_link}",
                file=e.stdout,
            )
    return 0


def cmd_prune(e: Env) -> int:
    """`prune`: trash the repository's expired artifacts, or only name them with `--dry-run`."""
    a = e.args
    ctx = _ctx(e, require_run=False)
    log.info(
        "looking for expired artifacts of %s%s", ctx.repo.slug, " (dry run)" if a.dry_run else ""
    )
    expired = _store(e).prune(ctx.repo.id, dry_run=a.dry_run)
    verb = "would trash" if a.dry_run else "trashed"
    e.actions.notice(f"{verb} {len(expired)} expired artifact(s)")
    _emit(e, [r.to_json() for r in expired])
    return 0


def cmd_login(e: Env) -> int:
    """`login`: the browser sign-in. The token goes into a GitHub secret through gh, or to stdout
    with `--print`."""
    a = e.args
    if e.settings.client_id == _client.CLIENT_ID and _client.CLIENT_ID.startswith("unset"):
        raise UsageError(
            "this build ships no OAuth client; "
            "set DRIVE_ARTIFACTS_CLIENT_ID and DRIVE_ARTIFACTS_CLIENT_SECRET first"
        )
    # Before any consent: a grant that cannot be stored would only have to be revoked again.
    # A missing gh raises its own UsageError from the runner.
    # One host only: a broken login on some other configured host is not this command's business.
    host = e.env.get("GH_HOST") or "github.com"
    if not a.print and e.run_gh(["auth", "status", "--active", "--hostname", host], "") != 0:
        raise UsageError(
            "gh is not authenticated; run `gh auth login`, "
            "or re-run with --print and add the secret by hand"
        )
    log.info("waiting for the browser login (up to %g s)", a.timeout)
    stored = loopback_login(e.http, e.settings, e.open_browser, timeout=a.timeout)
    text = stored.dumps()
    if a.print:
        print(text, file=e.stdout)
        return 0
    target = ["--repo", a.repo] if a.repo else ["--org", a.org]
    # A failure here leaves a grant nothing stored. It is not revoked: Google may answer a revoke
    # by ending the whole sign-in for this account and client, and that would also kill the token
    # every other repository already stores. An unused grant does no harm and lapses on its own.
    code = e.run_gh(["secret", "set", SECRET_NAME, *target], text)
    if code != 0:
        raise UsageError(
            f"`gh secret set` failed with exit {code}; the new token was not stored anywhere. "
            "Re-run with --print and add the secret by hand"
        )
    e.actions.notice(f"stored {SECRET_NAME} on {a.repo or a.org}")
    return 0


def cmd_logout(e: Env) -> int:
    """`logout`: revoke the grant behind the token in the environment."""
    stored = _stored(e)
    log.info("revoking the grant held in %s", SECRET_NAME)
    revoke(e.http, e.settings, stored.refresh_token)
    e.actions.notice(
        f"revoked the Drive grant; now delete the {SECRET_NAME} secret on GitHub. "
        "Other repositories whose token came from the same Google account may have to log in again"
    )
    return 0


def cmd_whoami(e: Env) -> int:
    """`whoami`: the client in use, whether a token is set, and the root folder if there is one."""
    has_token = e.settings.token_text is not None
    log.info(
        "looking up the root folder" if has_token else "no token set; skipping the root folder"
    )
    root = _store(e).root_folder(create=False) if has_token else None
    if e.args.json:
        folder = (
            None if root is None else {"id": root.id, "name": root.name, "url": root.web_view_link}
        )
        _emit(e, {"client_id": e.settings.client_id, "token": has_token, "root": folder})
    else:
        where = "none yet" if root is None else f"{root.name} ({root.id}) {root.web_view_link}"
        print(
            f"client: {e.settings.client_id}\n"
            f"token: {'set' if has_token else 'not set'}\n"
            f"root folder: {where}",
            file=e.stdout,
        )
    return 0


COMMANDS: dict[str, Callable[[Env], int]] = {
    "upload": cmd_upload,
    "download": cmd_download,
    "list": cmd_list,
    "prune": cmd_prune,
    "login": cmd_login,
    "logout": cmd_logout,
    "whoami": cmd_whoami,
}


def _default_open_browser(stderr: TextIO) -> Callable[[str], None]:
    def open_browser(url: str) -> None:
        print(
            f"Opening your browser to authorise drive-artifacts. If nothing opens, visit:\n{url}",
            file=stderr,
        )
        webbrowser.open(url)

    return open_browser


def _default_run_gh(args: list[str], stdin: str) -> int:
    gh = shutil.which("gh")
    if gh is None:
        raise UsageError("gh is not on PATH; re-run with --print and add the secret by hand")
    # The login probe is a yes/no question: its account listing stays off the terminal.
    quiet = subprocess.DEVNULL if args[:2] == ["auth", "status"] else None
    # The secret goes in on stdin, never in argv.
    try:
        return subprocess.run(  # noqa: S603 - argv list, no shell
            [gh, *args], input=stdin, text=True, check=False, stdout=quiet, stderr=quiet
        ).returncode
    except OSError as exc:
        raise UsageError(
            f"could not run gh ({exc}); re-run with --print and add the secret by hand"
        ) from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    cwd: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    open_browser: Callable[[str], None] | None = None,
    run_gh: Callable[[list[str], str], int] | None = None,
) -> int:
    """Run the CLI. Returns the process exit code."""
    environ: Mapping[str, str] = os.environ if env is None else env
    out: TextIO = sys.stdout if stdout is None else stdout
    err: TextIO = sys.stderr if stderr is None else stderr
    args = _parser().parse_args(argv)
    # A handler of this call's own on this package's logger: `basicConfig` would bind the root
    # logger to the first stream it ever saw, and do nothing at all once a host program has
    # configured logging. Removed again when the call ends.
    handler = logging.StreamHandler(err)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO if args.verbose else logging.WARNING)
    # httpx logs full request URLs at INFO; ours carry the upload session id.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    actions = Actions(environ, err)
    try:
        with httpx.Client(transport=transport) as http:
            e = Env(
                args=args,
                env=environ,
                settings=Settings.from_env(environ),
                actions=actions,
                stdout=out,
                stderr=err,
                cwd=(cwd or Path.cwd()).resolve(),
                http=http,
                open_browser=open_browser or _default_open_browser(err),
                run_gh=run_gh or _default_run_gh,
            )
            try:
                return COMMANDS[args.command](e)
            except DriveArtifactsError as exc:
                actions.error(str(exc))
                return exc.exit_code
            except BrokenPipeError:
                # `drive-artifacts list | head -1`: the reader left. Not an error of ours, and
                # there is nobody to tell; 141 is what a shell reports for a SIGPIPE death.
                return 141
            except OSError as exc:
                # A full disk, a read-only directory: the environment, not the artifact.
                actions.error(f"filesystem error: {exc}")
                return 1
            except KeyboardInterrupt:
                actions.error("interrupted")
                return 130
    finally:
        log.removeHandler(handler)
