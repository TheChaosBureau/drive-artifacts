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


def folder_properties(kind: Literal["root", "repo"], repo_id: str | None = None) -> dict[str, str]:
    """The `appProperties` of the root folder, or of one repository's folder."""
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
