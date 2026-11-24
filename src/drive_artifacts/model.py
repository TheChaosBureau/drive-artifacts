# SPDX-License-Identifier: Apache-2.0
"""Records, names and the Drive metadata that carries identity.

Nothing here talks to the network. Identity lives in `appProperties`, never in file
names or paths (Drive names are not unique and repositories get renamed).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from drive_artifacts.errors import UsageError

SCHEMA_VERSION = "1"
FOLDER_MIME = "application/vnd.google-apps.folder"
ZIP_MIME = "application/zip"
MAX_NAME_BYTES = 100
PROPERTY_LIMIT = 124  # Drive: key + value, UTF-8 bytes
FORBIDDEN = frozenset('":<>|*?\r\n/\\\x00')  # NUL: no path may hold it, and the name becomes one
RESERVED_NAMES = frozenset({".", ".."})
# `da.ref` is informational (a filter key, not identity), so a ref longer than this is
# stored as a digest instead of failing the upload. 117 is the controller's ruling.
REF_MAX_BYTES = 117
REF_DIGEST_PREFIX = "sha256:"
LATEST_FOLDER = "Latest"  # under the root: where "latest" files go unless a path is given
DEFAULT_MIME = "application/octet-stream"


def _utf8_len(text: str, what: str) -> int:
    """UTF-8 length of `text`. Argv and env values with invalid bytes decode to lone surrogates."""
    try:
        return len(text.encode())
    except UnicodeEncodeError as exc:
        raise UsageError(f"{what} is not valid UTF-8") from exc


def validate_artifact_name(name: str) -> str:
    """Mirror GitHub's artifact-name rules, plus a byte ceiling so it fits a property."""
    name = name.strip()
    if not name:
        raise UsageError("artifact name must not be empty")
    bad = sorted(FORBIDDEN & set(name))
    if bad:
        raise UsageError(f"artifact name contains forbidden characters: {''.join(bad)!r}")
    if name in RESERVED_NAMES:
        # Not a name but a path step: download-all uses the name as a directory under --path.
        raise UsageError(f"artifact name must not be {name!r}")
    if _utf8_len(name, "artifact name") > MAX_NAME_BYTES:
        raise UsageError(f"artifact name is longer than {MAX_NAME_BYTES} bytes")
    return name


def fit_ref(ref: str) -> str:
    """The `da.ref` value for `ref`: unchanged when it fits, else a stable digest of it.

    Writers and the query side must both go through this, so a long branch name still
    matches the file it was stored with.
    """
    if _utf8_len(ref, "ref") <= REF_MAX_BYTES:
        return ref
    return REF_DIGEST_PREFIX + hashlib.sha256(ref.encode()).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class Repo:
    """A repository: its numeric id, which survives a rename, and its `owner/name` for people."""

    id: str  # GitHub numeric repository id, rename-safe
    slug: str  # owner/name, for humans


@dataclass(frozen=True, slots=True)
class RunContext:
    """The run an artifact belongs to: repository, run id and attempt, ref and commit."""

    repo: Repo
    run_id: str
    run_attempt: str
    ref: str
    commit: str


@dataclass(frozen=True, slots=True)
class ArtifactKey:
    """What identifies one artifact: repository, run, attempt and name."""

    repo_id: str
    run_id: str
    run_attempt: str
    name: str

    @property
    def filename(self) -> str:
        """The zip's name in Drive: `<run id>-<attempt>-<name>.zip`."""
        return f"{self.run_id}-{self.run_attempt}-{self.name}.zip"


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One stored artifact as read back from Drive: its key, file id, digest, size and expiry."""

    key: ArtifactKey
    file_id: str
    sha256: str
    size: int
    ref: str
    commit: str
    expires: date
    web_view_link: str
    modified_time: str
    created_time: str = ""  # when the upload landed; unlike modified_time it never moves

    @classmethod
    def from_drive(cls, file: Mapping[str, object]) -> ArtifactRecord:
        """Parse a Drive file resource; every failure is a `ValueError` carrying the file id.

        A file that is not ours (no properties, another kind or another schema version) and a
        file that is ours but corrupt (missing property, bad date or size) both raise it, so
        a caller can `except ValueError: continue` over a whole listing.
        """
        props = file.get("appProperties")
        if (
            not isinstance(props, Mapping)
            or props.get("da.kind") != "artifact"
            or props.get("da.v") != SCHEMA_VERSION
        ):
            raise ValueError(f"not a drive-artifacts file: {file.get('id')!r}")
        p: Mapping[str, str] = props
        try:
            # The property is attacker-writable (anyone holding the token, or the API, can plant a
            # record), and the name becomes a directory on download: only a name `put` could have
            # stored, exactly as stored, gets through.
            name = p["da.name"]
            if validate_artifact_name(name) != name:
                raise ValueError(f"artifact name {name!r} is not in canonical form")
            return cls(
                key=ArtifactKey(p["da.repo"], p["da.run"], p["da.attempt"], name),
                file_id=str(file["id"]),
                sha256=p["da.sha256"],
                size=int(str(file.get("size", "0"))),
                ref=p.get("da.ref", ""),
                commit=p.get("da.commit", ""),
                expires=date.fromisoformat(p["da.expires"]),
                web_view_link=str(file.get("webViewLink", "")),
                modified_time=str(file.get("modifiedTime", "")),
                created_time=str(file.get("createdTime", "")),
            )
        except (KeyError, ValueError, UsageError) as exc:
            raise ValueError(f"malformed drive-artifacts file {file.get('id')!r}: {exc}") from exc

    def to_json(self) -> dict[str, object]:
        """The record as the CLI prints it with `--json`."""
        return {
            "name": self.key.name,
            "run_id": self.key.run_id,
            "run_attempt": self.key.run_attempt,
            "repo_id": self.key.repo_id,
            "file_id": self.file_id,
            "sha256": self.sha256,
            "size": self.size,
            "ref": self.ref,
            "commit": self.commit,
            "expires": self.expires.isoformat(),
            "url": self.web_view_link,
            "modified": self.modified_time,
            "created": self.created_time,
        }


def _path_step(step: str) -> str:
    """One folder or file name of a "latest" path, or a `UsageError` saying what is wrong with it."""
    if not step or step != step.strip():
        raise UsageError("a latest path must not hold an empty step or one with outer spaces")
    if step in RESERVED_NAMES:
        raise UsageError(f"a latest path must not hold the step {step!r}")
    if "\\" in step or any(ord(ch) < 32 or ord(ch) == 127 for ch in step):
        raise UsageError(f"a latest path step holds a backslash or a control character: {step!r}")
    _utf8_len(step, "latest path")
    return step


def latest_location(spec: str | None, repo_slug: str, filename: str) -> tuple[tuple[str, ...], str]:
    """Where a "latest" file goes under the root: its folders, outermost first, and its name.

    Without `spec` that is `Latest/<owner>/<repo>/<filename>`. A `spec` is a `/`-separated path
    relative to the root; one that ends in `/` names a folder, and the file keeps its own name.
    """
    if spec is None or not spec.strip():
        steps = [LATEST_FOLDER, *repo_slug.split("/"), filename]
    else:
        spec = spec.strip()
        steps = spec.split("/")
        if spec.endswith("/"):
            steps[-1] = filename
    checked = tuple(_path_step(step) for step in steps)
    return checked[:-1], checked[-1]


@dataclass(frozen=True, slots=True)
class LatestRecord:
    """One "latest" file as read back from Drive: where it is, what it was built from and when
    it was last replaced."""

    file_id: str
    name: str
    folder: str  # the folders under the root, joined with `/`; empty for the root itself
    repo_id: str
    repo: str  # owner/name at the time of the upload
    run_id: str
    run_attempt: str
    ref: str
    commit: str
    sha256: str
    uploaded: str  # UTC, ISO 8601: when the content was last replaced
    size: int
    web_view_link: str
    created_time: str
    modified_time: str

    @property
    def path(self) -> str:
        """The path under the root, file name included."""
        return f"{self.folder}/{self.name}" if self.folder else self.name

    @classmethod
    def from_drive(cls, file: Mapping[str, object]) -> LatestRecord:
        """Parse a Drive file resource; every failure is a `ValueError` carrying the file id, as
        with `ArtifactRecord.from_drive`."""
        props = file.get("appProperties")
        if (
            not isinstance(props, Mapping)
            or props.get("da.kind") != "latest"
            or props.get("da.v") != SCHEMA_VERSION
        ):
            raise ValueError(f"not a drive-artifacts latest file: {file.get('id')!r}")
        p: Mapping[str, str] = props
        try:
            return cls(
                file_id=str(file["id"]),
                name=str(file["name"]),
                folder=p["da.dir"],
                repo_id=p["da.repo"],
                repo=p["da.slug"],
                run_id=p["da.run"],
                run_attempt=p["da.attempt"],
                ref=p.get("da.ref", ""),
                commit=p.get("da.commit", ""),
                sha256=p["da.sha256"],
                uploaded=p["da.uploaded"],
                size=int(str(file.get("size", "0"))),
                web_view_link=str(file.get("webViewLink", "")),
                created_time=str(file.get("createdTime", "")),
                modified_time=str(file.get("modifiedTime", "")),
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"malformed drive-artifacts latest file {file.get('id')!r}: {exc}"
            ) from exc

    def to_json(self) -> dict[str, object]:
        """The record as the CLI prints it with `--json`."""
        return {
            "path": self.path,
            "name": self.name,
            "url": self.web_view_link,
            "file_id": self.file_id,
            "repo": self.repo,
            "repo_id": self.repo_id,
            "commit": self.commit,
            "ref": self.ref,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "uploaded": self.uploaded,
            "sha256": self.sha256,
            "size": self.size,
            "created": self.created_time,
            "modified": self.modified_time,
        }


def check_properties(props: Mapping[str, str]) -> dict[str, str]:
    """Refuse a property whose key and value together exceed Drive's limit; return a copy."""
    for key, value in props.items():
        what = f"property {key!r}"
        if _utf8_len(key, what) + _utf8_len(value, what) > PROPERTY_LIMIT:
            raise UsageError(f"{what} exceeds Drive's {PROPERTY_LIMIT}-byte limit")
    return dict(props)


def artifact_properties(
    key: ArtifactKey, *, sha256: str, ref: str, commit: str, expires: date
) -> dict[str, str]:
    """The `appProperties` that make a Drive file an artifact: identity, digest, ref, commit and
    expiry."""
    return check_properties(
        {
            "da.v": SCHEMA_VERSION,
            "da.kind": "artifact",
            "da.repo": key.repo_id,
            "da.run": key.run_id,
            "da.attempt": key.run_attempt,
            "da.name": key.name,
            "da.sha256": sha256,
            "da.ref": fit_ref(ref),
            "da.commit": commit,
            "da.expires": expires.isoformat(),
        }
    )


def latest_properties(
    ctx: RunContext, *, folder: str, sha256: str, uploaded: str
) -> dict[str, str]:
    """The `appProperties` that make a Drive file a "latest" file: where it sits under the root,
    and the repository, run, ref, commit, digest and time of the upload that last replaced it."""
    return check_properties(
        {
            "da.v": SCHEMA_VERSION,
            "da.kind": "latest",
            "da.dir": folder,
            "da.repo": ctx.repo.id,
            "da.slug": ctx.repo.slug,
            "da.run": ctx.run_id,
            "da.attempt": ctx.run_attempt,
            "da.ref": fit_ref(ctx.ref),
            "da.commit": ctx.commit,
            "da.sha256": sha256,
            "da.uploaded": uploaded,
        }
    )


def folder_properties(
    kind: Literal["root", "repo", "dir"], repo_id: str | None = None
) -> dict[str, str]:
    """The `appProperties` of the root folder, of one repository's folder, or of a folder on a
    "latest" path."""
    props = {"da.v": SCHEMA_VERSION, "da.kind": kind}
    if kind == "repo":
        if repo_id is None:
            raise UsageError("repo folders need a repo id")
        props["da.repo"] = repo_id
    return check_properties(props)


def escape(value: str) -> str:
    """Escape a value for a Drive `q` string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def prop(key: str, value: str) -> str:
    """A Drive `q` term that matches one app property exactly."""
    return f"appProperties has {{ key='{escape(key)}' and value='{escape(value)}' }}"


def query(*terms: str) -> str:
    """Join `q` terms with `and`, and leave trashed files out."""
    return " and ".join([*terms, "trashed = false"])
