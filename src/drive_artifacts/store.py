# SPDX-License-Identifier: Apache-2.0
"""The artifact contract: how artifacts are addressed, stored, found, verified and expired."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from drive_artifacts.archive import sha256_file
from drive_artifacts.drive import DriveClient, DriveFile
from drive_artifacts.errors import (
    ConflictError,
    DriveArtifactsError,
    DriveError,
    IntegrityError,
    UsageError,
)
from drive_artifacts.model import (
    FOLDER_MIME,
    ArtifactKey,
    ArtifactRecord,
    Repo,
    RunContext,
    artifact_properties,
    escape,
    fit_ref,
    folder_properties,
    prop,
    query,
    validate_artifact_name,
)

MAX_RETENTION_DAYS = 400


def validate_retention_days(days: int) -> int:
    """The one rule for how long an artifact may be kept; the CLI and the store both use it."""
    if not 1 <= days <= MAX_RETENTION_DAYS:
        raise UsageError(f"retention days must be 1..{MAX_RETENTION_DAYS}, got {days}")
    return days


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _oldest(files: list[DriveFile]) -> DriveFile | None:
    return min(files, key=lambda f: (f.created_time, f.id)) if files else None


class ArtifactStore:
    """Artifacts in Drive: one root folder, one folder per repository, one zip per artifact,
    identity in `appProperties`."""

    def __init__(
        self, drive: DriveClient, *, root_name: str, today: Callable[[], date] = _utc_today
    ) -> None:
        self._drive = drive
        self._root_name = root_name
        self._today = today

    # ----- folders -----------------------------------------------------------
    def _folder(self, q: str, create: Callable[[], DriveFile] | None) -> DriveFile | None:
        """Query, create, re-query. Two racing creators converge on the oldest folder."""
        found = _oldest(self._drive.list_files(q))
        if found is not None or create is None:
            return found
        mine = create()
        winner = _oldest(self._drive.list_files(q)) or mine
        # Trash ours only while it is still empty: a racer that picked it before the older
        # folder appeared may already have uploaded into it.
        if winner.id != mine.id and not self._drive.list_files(
            query(f"'{escape(mine.id)}' in parents")
        ):
            self._drive.trash(mine.id)
        return winner

    def root_folder(self, *, create: bool) -> DriveFile | None:
        """The root folder; created when it is missing and `create` is true, otherwise `None`."""
        q = query(
            prop("da.kind", "root"),
            f"mimeType = '{FOLDER_MIME}'",
            f"name = '{escape(self._root_name)}'",
        )
        maker = (
            (
                lambda: self._drive.create_folder(
                    self._root_name, parent=None, app_properties=folder_properties("root")
                )
            )
            if create
            else None
        )
        return self._folder(q, maker)

    def repo_folder(self, repo: Repo, *, create: bool) -> DriveFile | None:
        """The repository's folder under the root; created when it is missing and `create` is
        true, otherwise `None`."""
        root = self.root_folder(create=create)
        if root is None:
            return None
        q = query(
            prop("da.kind", "repo"),
            prop("da.repo", repo.id),
            f"'{escape(root.id)}' in parents",
            f"mimeType = '{FOLDER_MIME}'",
        )
        maker = (
            (
                lambda: self._drive.create_folder(
                    repo.slug, parent=root.id, app_properties=folder_properties("repo", repo.id)
                )
            )
            if create
            else None
        )
        return self._folder(q, maker)

    # ----- artifacts ---------------------------------------------------------
    def _records(
        self, *terms: str, order_by: str | None = None, max_pages: int | None = None
    ) -> list[ArtifactRecord]:
        files = self._drive.list_files(
            query(prop("da.kind", "artifact"), *terms), order_by=order_by, max_pages=max_pages
        )
        records: list[ArtifactRecord] = []
        for file in files:
            try:
                records.append(ArtifactRecord.from_drive(file.as_mapping()))
            except ValueError:
                continue  # another schema version or a corrupt file: not ours to act on
        return records

    def put(
        self,
        ctx: RunContext,
        name: str,
        zip_path: Path,
        *,
        sha256: str,
        size: int,
        retention_days: int,
        overwrite: bool = False,
    ) -> ArtifactRecord:
        """Upload a zip as the artifact `name` of this run and return its record.

        An existing artifact of that name raises `ConflictError` unless `overwrite` is set. An
        upload whose size Drive records differently is trashed and raises `DriveError`.
        """
        name = validate_artifact_name(name)
        validate_retention_days(retention_days)
        key = ArtifactKey(ctx.repo.id, ctx.run_id, ctx.run_attempt, name)
        existing = self.find(key)
        if existing is not None and not overwrite:
            raise ConflictError(
                f"artifact {name!r} already exists for run {ctx.run_id} attempt {ctx.run_attempt} "
                f"(Drive file {existing.file_id}); pass overwrite to replace it"
            )
        folder = self.repo_folder(ctx.repo, create=True)
        if folder is None:  # pragma: no cover - create=True never yields None
            raise DriveError("could not resolve the repository folder")
        props = artifact_properties(
            key,
            sha256=sha256,
            ref=ctx.ref,
            commit=ctx.commit,
            expires=self._today() + timedelta(days=retention_days),
        )
        uploaded = self._drive.upload_file(
            zip_path, name=key.filename, parent=folder.id, app_properties=props
        )
        if (
            uploaded.size is None
            or not uploaded.created_time
            or not uploaded.web_view_link
            or not uploaded.app_properties
        ):
            # The last chunk's answer carries these only when the session's `fields` reached it;
            # read the file rather than check sizes (or order uploads) against blanks.
            try:
                uploaded = self._drive.get_file(uploaded.id)
            except DriveArtifactsError:
                # Stored but unverified: the same rule as a size mismatch below.
                with suppress(DriveArtifactsError):
                    self._drive.trash(uploaded.id)
                raise
        record = ArtifactRecord.from_drive(uploaded.as_mapping())
        if record.size != size:
            # Never leave a half-good artifact behind: it would make a plain retry conflict.
            # Trashing is best effort; a failure to trash must not mask the real error.
            with suppress(DriveArtifactsError):
                self._drive.trash(uploaded.id)
            raise DriveError(
                f"Drive recorded {record.size} bytes for a {size}-byte upload",
                reason="sizeMismatch",
            )
        if overwrite:
            # Only after the replacement is complete and verified. Every live record for the key
            # uploaded before this one goes, not just the one seen first (a lost response can
            # leave duplicates); a newer one belongs to a racing overwrite and wins, as in `find`.
            mine = (record.created_time, record.file_id)
            stale = [] if existing is None else [existing.file_id]
            stale += [
                r.file_id
                for r in self._key_records(key)
                if (r.created_time, r.file_id) < mine and r.file_id not in stale
            ]
            for file_id in stale:
                self._drive.trash(file_id)
        return record

    def _key_records(self, key: ArtifactKey) -> list[ArtifactRecord]:
        return self._records(
            prop("da.repo", key.repo_id),
            prop("da.run", key.run_id),
            prop("da.attempt", key.run_attempt),
            prop("da.name", key.name),
        )

    def find(self, key: ArtifactKey) -> ArtifactRecord | None:
        """The newest live record for a key, or `None`."""
        records = self._key_records(key)
        # the last upload wins; modifiedTime also moves on a trash/untrash, createdTime never does
        return max(records, key=lambda r: (r.created_time, r.file_id)) if records else None

    def list_run(
        self, repo_id: str, run_id: str, *, run_attempt: str | None = None
    ) -> list[ArtifactRecord]:
        """A run's artifacts, by name and then upload time; every attempt unless `run_attempt` is
        given."""
        terms = [prop("da.repo", repo_id), prop("da.run", run_id)]
        if run_attempt is not None:
            terms.append(prop("da.attempt", run_attempt))
        return sorted(self._records(*terms), key=lambda r: (r.key.name, r.created_time))

    def list_repo(self, repo_id: str, *, ref: str | None = None) -> list[ArtifactRecord]:
        """Every artifact of the repository, newest first, optionally for one ref."""
        terms = [prop("da.repo", repo_id)]
        if ref is not None:
            # the same mapping artifact_properties applied on upload
            terms.append(prop("da.ref", fit_ref(ref)))
        return self._records(*terms, order_by="createdTime desc")

    def latest(
        self, repo_id: str, name: str, *, ref: str | None = None, commit: str | None = None
    ) -> ArtifactRecord | None:
        """The newest artifact of that name in the repository, optionally for one ref or commit."""
        terms = [prop("da.repo", repo_id), prop("da.name", name)]
        if ref is not None:
            # the same mapping artifact_properties applied on upload
            terms.append(prop("da.ref", fit_ref(ref)))
        if commit is not None:
            terms.append(prop("da.commit", commit))
        # Newest first, so the answer is on the first page: one request, however many there are.
        records = self._records(*terms, order_by="createdTime desc", max_pages=1)
        return records[0] if records else None

    def fetch(self, record: ArtifactRecord, dest_zip: Path) -> None:
        """Download a record's zip to `dest_zip` and verify its digest. A mismatch deletes the
        file and raises `IntegrityError`."""
        self._drive.download_file(record.file_id, dest_zip, expected_size=record.size)
        actual = sha256_file(dest_zip)
        if actual != record.sha256:
            dest_zip.unlink(missing_ok=True)
            raise IntegrityError(
                f"digest mismatch for {record.key.name!r}: "
                f"Drive holds {actual}, the record says {record.sha256}"
            )

    def prune(self, repo_id: str, *, dry_run: bool = False) -> list[ArtifactRecord]:
        """Trash the repository's artifacts whose expiry date has passed, or only find them with
        `dry_run`; return them."""
        today = self._today()
        expired = [r for r in self._records(prop("da.repo", repo_id)) if r.expires < today]
        if not dry_run:
            for record in expired:
                self._drive.trash(record.file_id)
        return expired
