# SPDX-License-Identifier: Apache-2.0
"""Which files an artifact holds, how they become one zip, and how that zip is opened safely."""

from __future__ import annotations

import glob
import hashlib
import lzma
import os
import re
import stat
import zipfile
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from drive_artifacts.errors import IntegrityError, UsageError

FIXED_DATE = (1980, 1, 1, 0, 0, 0)  # deterministic archives: same tree, same digest
CHUNK = 1 << 20
_MAGIC = re.compile(r"[*?\[]")
_DRIVE = re.compile(r"[A-Za-z]:")  # a Windows drive prefix; other colons are legal POSIX names


@dataclass(frozen=True, slots=True)
class Resolved:
    files: list[Path]
    empty_dirs: list[Path]
    root: Path


@dataclass(frozen=True, slots=True)
class ZipResult:
    path: Path
    size: int
    sha256: str
    file_count: int


def _norm(path: Path | str) -> Path:
    """Absolute and lexically normalised. Symlinks are never resolved: a link keeps its own name."""
    return Path(os.path.abspath(path))  # noqa: PTH100 - Path.resolve() would follow symlinks


def _is_hidden(path: Path, base: Path) -> bool:
    """A dot-name below `base` hides `path`; `base` itself and everything above it never count."""
    try:
        parts = path.relative_to(base).parts
    except ValueError:  # a pattern that climbs out of its search path with `..`: the name speaks
        parts = path.parts[-1:]
    return any(part.startswith(".") for part in parts)


def _expand(path: Path, *, include_hidden: bool, base: Path) -> tuple[set[Path], set[Path]]:
    """A file is itself; a directory is every file below it plus its empty directories.

    Links keep their own names. A link to a file is that file's content under the link's name; a
    broken link (or a socket, fifo, device) is dropped; a link to a directory is not descended into
    unless a pattern names it, which makes it the top of the walk.
    """
    if path.is_file():
        return ({path}, set())
    files: set[Path] = set()
    empty: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(path):
        here = Path(dirpath)
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            filenames = [f for f in filenames if not f.startswith(".")]
        if not include_hidden and _is_hidden(here, base):
            continue
        filenames = [f for f in filenames if (here / f).is_file()]
        for name in filenames:
            files.add(here / name)
        if not filenames and not dirnames and here != path:
            empty.add(here)
    return files, empty


def _matches(pattern: str, cwd: Path, *, include_hidden: bool) -> tuple[set[Path], set[Path]]:
    # Not Path.glob: it matches dotfiles unconditionally and rejects absolute patterns.
    hits = glob.glob(pattern, root_dir=cwd, recursive=True, include_hidden=include_hidden)  # noqa: PTH207
    base = _search_base(pattern, cwd)  # hiddenness is judged below the search path only
    files: set[Path] = set()
    empty: set[Path] = set()
    for hit in hits:
        p = _norm(cwd / hit)
        if not include_hidden and _is_hidden(p, base):
            continue
        f, e = _expand(p, include_hidden=include_hidden, base=base)
        files |= f
        empty |= e
    return files, empty


def resolve_paths(patterns: Sequence[str], cwd: Path, *, include_hidden: bool = False) -> Resolved:
    """GitHub `path` semantics: one pattern per line, `!` excludes, `#` comments, directories expand."""
    cwd = _norm(cwd)
    lines = [line for raw in patterns if (line := raw.strip()) and not line.startswith("#")]
    includes = [line for line in lines if not line.startswith("!")]
    excludes = [line[1:].strip() for line in lines if line.startswith("!")]
    files: set[Path] = set()
    empty: set[Path] = set()
    excluded: set[Path] = set()
    for pattern in includes:
        f, e = _matches(pattern, cwd, include_hidden=include_hidden)
        files |= f
        empty |= e
    for pattern in excludes:
        ex_files, ex_empty = _matches(pattern, cwd, include_hidden=include_hidden)
        excluded |= ex_files | ex_empty
    files -= excluded
    empty = {d for d in empty if d not in excluded and not any(f.is_relative_to(d) for f in files)}
    if not files and not empty:
        return Resolved([], [], cwd)
    root = Path(os.path.commonpath([str(_search_base(p, cwd)) for p in includes]))
    return Resolved(sorted(files), sorted(empty), root)


def _search_base(pattern: str, cwd: Path) -> Path:
    """GitHub's search path: the pattern's literal prefix, whether or not it exists, so the archive
    layout never depends on what happens to be on disk; the parent when the prefix is a file."""
    parts: list[str] = []
    for part in Path(pattern.strip()).parts:
        if _MAGIC.search(part):
            break
        parts.append(part)
    base = _norm(cwd / Path(*parts)) if parts else cwd
    return base.parent if base.is_file() else base


def sha256_file(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _arcname(path: Path, root: Path) -> str:
    """The name `path` gets in the zip. Refuse here what `safe_extract` would refuse there."""
    name = path.relative_to(root).as_posix()
    if "\\" in name or _DRIVE.match(name.split("/", 1)[0]):
        raise UsageError(
            f"cannot archive {path}: extraction refuses the name {name!r} "
            "(it holds a backslash or starts with a drive letter)"
        )
    return name


def build_zip(resolved: Resolved, dest: Path, *, compression_level: int = 6) -> ZipResult:
    """One deterministic zip: sorted posix arcnames, fixed timestamps, mode bits kept."""
    if not 0 <= compression_level <= 9:
        raise ValueError("compression_level must be 0..9")
    method = zipfile.ZIP_STORED if compression_level == 0 else zipfile.ZIP_DEFLATED
    entries: list[tuple[str, Path | None]] = []
    entries.extend((_arcname(p, resolved.root), p) for p in resolved.files)
    entries.extend((_arcname(d, resolved.root) + "/", None) for d in resolved.empty_dirs)
    entries.sort(key=lambda e: e[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        dest, "w", compression=method, compresslevel=compression_level or None
    ) as zf:
        for arcname, src in entries:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE)
            if src is None:
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                zf.writestr(info, b"")
                continue
            meta = src.stat()
            info.external_attr = (stat.S_IFREG | stat.S_IMODE(meta.st_mode)) << 16
            info.file_size = meta.st_size  # streamed entries need the size up front to choose zip64
            info.compress_type = method
            # A hand-built ZipInfo does not inherit the ZipFile's level and zipfile has no public
            # setter for it (the typeshed stubs do not know the attribute, hence the cast).
            cast("Any", info)._compresslevel = compression_level
            with src.open("rb") as fh, zf.open(info, "w") as out:
                while chunk := fh.read(CHUNK):
                    out.write(chunk)
    return ZipResult(dest, dest.stat().st_size, sha256_file(dest), len(resolved.files))


# What decoding a member can raise: a bad CRC or header, a truncated or garbled stream, a method we
# cannot undo, encryption, or a name that is not valid UTF-8. All of them mean "this archive is bad".
_DECODE_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
    EOFError,
    NotImplementedError,
    RuntimeError,
    UnicodeDecodeError,
)


def _check_member(name: str, dest: Path) -> Path:
    pure = PurePosixPath(name)
    if (
        not name
        or name.startswith(("/", "\\"))
        or ".." in pure.parts
        or any(_DRIVE.match(part) for part in pure.parts[:1])
        or "\\" in name
        or (not pure.parts and not name.endswith("/"))  # "." names the destination, not a file
    ):
        raise IntegrityError(f"unsafe path in archive: {name!r}")
    target = (dest / Path(*pure.parts)).resolve()
    if not target.is_relative_to(dest):
        raise IntegrityError(f"unsafe path in archive: {name!r}")
    return target


def _plan(infos: list[zipfile.ZipInfo], dest: Path) -> list[tuple[zipfile.ZipInfo, Path]]:
    """Validate every member before anything is written: unsafe paths, repeated names, and a file
    that is also a directory prefix of another member all raise here, never half-way through."""
    plan: list[tuple[zipfile.ZipInfo, Path]] = []
    seen: set[tuple[str, ...]] = set()
    files: set[tuple[str, ...]] = set()
    for info in infos:
        target = _check_member(info.filename, dest)
        key = PurePosixPath(info.filename).parts
        if key:  # () is the destination itself
            if key in seen:
                raise IntegrityError(
                    f"archive members conflict: {info.filename!r} repeats an earlier member"
                )
            seen.add(key)
            if not info.is_dir():
                files.add(key)
        plan.append((info, target))
    for info, _ in plan:
        key = PurePosixPath(info.filename).parts
        for depth in range(1, len(key)):
            if key[:depth] in files:
                raise IntegrityError(
                    f"archive members conflict: {info.filename!r} lies under "
                    f"{'/'.join(key[:depth])!r}, which the archive holds as a file"
                )
    return plan


def _decoded_chunks(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[bytes]:
    """The member's bytes; only failures of the archive itself become IntegrityError."""
    try:
        with zf.open(info) as src:
            while chunk := src.read(CHUNK):
                yield chunk
    except (*_DECODE_ERRORS, OSError) as exc:  # nothing is being written here; bz2 raises OSError
        raise IntegrityError(f"corrupt or unextractable member {info.filename!r}: {exc}") from exc


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    """Write errors (full disk, permissions) are real OSErrors and propagate unwrapped."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as out:
        for chunk in _decoded_chunks(zf, info):
            out.write(chunk)
    mode = info.external_attr >> 16
    regular = stat.S_IFMT(mode) in (0, stat.S_IFREG)  # a symlink entry is just text here
    # Windows honours only the read-only flag of chmod, and both values leave the file writable.
    target.chmod(0o755 if regular and mode & 0o111 else 0o644)


def safe_extract(
    zip_path: Path,
    dest: Path,
    *,
    max_members: int = 200_000,
    max_total_bytes: int = 64 * 2**30,
) -> list[Path]:
    """Extract with zip-slip and zip-bomb guards. Returns the files written.

    Every member is validated before the first byte is written.
    """
    dest = dest.resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise IntegrityError(f"not a readable zip archive: {zip_path} ({exc})") from exc
    written: list[Path] = []
    with zf:
        infos = zf.infolist()
        if len(infos) > max_members:
            raise IntegrityError(f"archive has {len(infos)} members, more than {max_members}")
        total = sum(i.file_size for i in infos)
        if total > max_total_bytes:
            raise IntegrityError(f"archive expands to {total} bytes, more than {max_total_bytes}")
        plan = _plan(infos, dest)
        dest.mkdir(parents=True, exist_ok=True)
        for info, target in plan:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                _write_member(zf, info, target)
                written.append(target)
    return written
