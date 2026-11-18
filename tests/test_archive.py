# SPDX-License-Identifier: Apache-2.0
import lzma
import os
import stat
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from drive_artifacts.archive import build_zip, resolve_paths, safe_extract, sha256_file
from drive_artifacts.errors import IntegrityError, UsageError


def write_zip(path: Path, members: list[str]) -> None:
    """A zip whose members are empty; a name ending in `/` is a directory entry."""
    with zipfile.ZipFile(path, "w") as zf:
        for name in members:
            zf.writestr(name, b"")


def make_tree(root: Path, files: dict[str, bytes], dirs: tuple[str, ...] = ()) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)


def test_directory_expands_recursively_and_root_is_the_directory(tmp_path: Path) -> None:
    make_tree(tmp_path, {"dist/a.whl": b"a", "dist/sub/b.txt": b"b", "other.txt": b"o"})
    r = resolve_paths(["dist/"], tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in r.files] == ["dist/a.whl", "dist/sub/b.txt"]
    assert r.root == tmp_path / "dist"


def test_globs_excludes_and_common_root(tmp_path: Path) -> None:
    make_tree(
        tmp_path,
        {"dist/a.whl": b"a", "dist/a.tar.gz": b"t", "build/x.log": b"x", "build/keep.txt": b"k"},
    )
    r = resolve_paths(["dist/*.whl", "build/**", "!build/*.log"], tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in r.files] == ["build/keep.txt", "dist/a.whl"]
    assert r.root == tmp_path


def test_single_file_root_is_its_parent(tmp_path: Path) -> None:
    make_tree(tmp_path, {"dist/a.whl": b"a"})
    r = resolve_paths(["dist/a.whl"], tmp_path)
    assert r.files == [tmp_path / "dist" / "a.whl"]
    assert r.root == tmp_path / "dist"


def test_hidden_entries_are_skipped_unless_asked(tmp_path: Path) -> None:
    make_tree(tmp_path, {"d/.env": b"s", "d/.git/x": b"g", "d/ok": b"o"})
    assert [p.name for p in resolve_paths(["d"], tmp_path).files] == ["ok"]
    assert len(resolve_paths(["d"], tmp_path, include_hidden=True).files) == 3


def test_empty_dirs_are_recorded(tmp_path: Path) -> None:
    make_tree(tmp_path, {"d/a": b"a"}, dirs=("d/empty", "d/.hidden_empty"))
    r = resolve_paths(["d"], tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in r.empty_dirs] == ["d/empty"]


def test_no_match_is_empty(tmp_path: Path) -> None:
    r = resolve_paths(["nothing/**", "", "   "], tmp_path)
    assert (r.files, r.empty_dirs, r.root) == ([], [], tmp_path)


def test_zip_round_trip_is_deterministic_and_verifiable(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_tree(src, {"d/a.txt": b"hello", "d/sub/b.bin": bytes(range(256))}, dirs=("d/empty",))
    r = resolve_paths(["d"], src)
    z1 = build_zip(r, tmp_path / "one.zip")
    z2 = build_zip(r, tmp_path / "two.zip")
    assert z1.sha256 == z2.sha256 == sha256_file(tmp_path / "one.zip")
    assert z1.file_count == 2
    assert z1.size == (tmp_path / "one.zip").stat().st_size
    with zipfile.ZipFile(tmp_path / "one.zip") as zf:
        assert zf.namelist() == ["a.txt", "empty/", "sub/b.bin"]
    out = tmp_path / "out"
    extracted = safe_extract(tmp_path / "one.zip", out)
    assert sorted(p.relative_to(out).as_posix() for p in extracted) == ["a.txt", "sub/b.bin"]
    assert (out / "a.txt").read_bytes() == b"hello"
    assert (out / "sub" / "b.bin").read_bytes() == bytes(range(256))
    assert (out / "empty").is_dir()


def test_stored_when_level_zero(tmp_path: Path) -> None:
    make_tree(tmp_path / "s", {"a": b"a" * 1000})
    build_zip(resolve_paths(["a"], tmp_path / "s"), tmp_path / "z.zip", compression_level=0)
    with zipfile.ZipFile(tmp_path / "z.zip") as zf:
        assert zf.getinfo("a").compress_type == zipfile.ZIP_STORED


@pytest.mark.skipif(sys.platform == "win32", reason="no executable bit on Windows")
def test_executable_bit_survives(tmp_path: Path) -> None:
    make_tree(tmp_path / "s", {"run.sh": b"#!/bin/sh\n"})
    (tmp_path / "s" / "run.sh").chmod(0o755)
    build_zip(resolve_paths(["run.sh"], tmp_path / "s"), tmp_path / "z.zip")
    safe_extract(tmp_path / "z.zip", tmp_path / "o")
    assert os.access(tmp_path / "o" / "run.sh", os.X_OK)


@pytest.mark.parametrize(
    "name", ["../evil", "/abs", "a/../../b", "..", "C:\\x", "C:x", "c:/x", "./D:x"]
)
def test_zip_slip_is_rejected(tmp_path: Path, name: str) -> None:
    with zipfile.ZipFile(tmp_path / "bad.zip", "w") as zf:
        zf.writestr(name, b"x")
    with pytest.raises(IntegrityError):
        safe_extract(tmp_path / "bad.zip", tmp_path / "o")
    # Whatever the name aimed at, nothing was written: not beside the destination, not in it.
    assert sorted(p.name for p in tmp_path.iterdir()) in (["bad.zip"], ["bad.zip", "o"])
    assert list((tmp_path / "o").rglob("*")) == []


def test_limits_are_enforced(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "many.zip", "w") as zf:
        for i in range(5):
            zf.writestr(f"f{i}", b"0" * 10)
    with pytest.raises(IntegrityError, match="members"):
        safe_extract(tmp_path / "many.zip", tmp_path / "o", max_members=4)
    with pytest.raises(IntegrityError, match="bytes"):
        safe_extract(tmp_path / "many.zip", tmp_path / "o2", max_total_bytes=49)


def test_corrupt_zip_is_an_integrity_error(tmp_path: Path) -> None:
    (tmp_path / "c.zip").write_bytes(b"not a zip")
    with pytest.raises(IntegrityError):
        safe_extract(tmp_path / "c.zip", tmp_path / "o")


def test_the_destination_itself_is_a_harmless_directory_entry(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "dot.zip", "w") as zf:
        zf.writestr("./", b"")
        zf.writestr("a.txt", b"a")
    extracted = safe_extract(tmp_path / "dot.zip", tmp_path / "o")
    assert [p.name for p in extracted] == ["a.txt"]


def test_a_file_named_dot_is_rejected(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "dot.zip", "w") as zf:
        zf.writestr(".", b"x")
    with pytest.raises(IntegrityError):
        safe_extract(tmp_path / "dot.zip", tmp_path / "o")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlink_planted_in_the_destination_cannot_redirect_a_write(tmp_path: Path) -> None:
    outside, out = tmp_path / "outside", tmp_path / "o"
    outside.mkdir()
    out.mkdir()
    (out / "link").symlink_to(outside, target_is_directory=True)
    with zipfile.ZipFile(tmp_path / "s.zip", "w") as zf:
        zf.writestr("link/pwn", b"x")
    with pytest.raises(IntegrityError):
        safe_extract(tmp_path / "s.zip", out)
    assert not (outside / "pwn").exists()


def test_members_without_unix_mode_bits_are_extracted_with_default_permissions(
    tmp_path: Path,
) -> None:
    info = zipfile.ZipInfo("plain", date_time=(1980, 1, 1, 0, 0, 0))
    info.external_attr = 0x01  # DOS read-only flag only, as Windows zip tools write it
    with zipfile.ZipFile(tmp_path / "m.zip", "w") as zf:
        zf.writestr(info, b"p")
    extracted = safe_extract(tmp_path / "m.zip", tmp_path / "o")
    assert [p.read_bytes() for p in extracted] == [b"p"]


def test_a_hidden_file_named_alone_is_skipped_unless_asked(tmp_path: Path) -> None:
    make_tree(tmp_path, {".env": b"s", "ok": b"o"})
    assert resolve_paths([".env"], tmp_path).files == []
    assert resolve_paths([".env"], tmp_path, include_hidden=True).files == [tmp_path / ".env"]


def test_hidden_entries_are_judged_below_the_search_path_only(tmp_path: Path) -> None:
    make_tree(tmp_path, {".cache/x": b"x", ".cache/.y": b"y"})
    assert [p.name for p in resolve_paths([".cache"], tmp_path).files] == ["x"]
    assert [p.name for p in resolve_paths([".cache"], tmp_path, include_hidden=True).files] == [
        ".y",
        "x",
    ]


@pytest.mark.parametrize("pattern", ["{out}/", "{out}", "{out}/*", "{out}/**"])
def test_a_hidden_ancestor_of_an_absolute_pattern_does_not_hide_its_matches(
    tmp_path: Path, pattern: str
) -> None:
    make_tree(tmp_path, {".hidden-parent/out/ok.txt": b"o", ".hidden-parent/out/.secret": b"s"})
    out = tmp_path / ".hidden-parent" / "out"
    (tmp_path / "work").mkdir()
    spelled = [pattern.format(out=out.as_posix())]
    visible = resolve_paths(spelled, tmp_path / "work")
    assert [p.name for p in visible.files] == ["ok.txt"]
    assert visible.root == out
    everything = resolve_paths(spelled, tmp_path / "work", include_hidden=True)
    assert sorted(p.name for p in everything.files) == [".secret", "ok.txt"]


def test_a_hidden_cwd_does_not_hide_relative_matches(tmp_path: Path) -> None:
    cwd = tmp_path / ".workspace"
    make_tree(cwd, {"dist/a": b"a", "dist/.b": b"b"})
    assert [p.name for p in resolve_paths(["dist"], cwd).files] == ["a"]


def test_a_pattern_that_climbs_out_of_its_search_path_is_judged_by_the_matched_name(
    tmp_path: Path,
) -> None:
    make_tree(tmp_path, {"a/s/x": b"x", "b": b"b", ".b": b"h"})
    assert resolve_paths(["a/*/../../b"], tmp_path).files == [tmp_path / "b"]
    assert resolve_paths(["a/*/../../.b"], tmp_path).files == []
    assert resolve_paths(["a/*/../../.b"], tmp_path, include_hidden=True).files == [tmp_path / ".b"]


@pytest.fixture
def linked_tree(tmp_path: Path) -> Path:
    """src/dist holds a real file and three links: to a file outside src, to nothing, to a directory."""
    if sys.platform == "win32":
        pytest.skip("symlinks need privileges on Windows")
    src = tmp_path / "src"
    make_tree(tmp_path, {"outside/target.txt": b"payload", "outside_dir/x": b"x"})
    make_tree(src, {"dist/real.txt": b"real"})
    (src / "dist" / "link").symlink_to(tmp_path / "outside" / "target.txt")
    (src / "dist" / "broken").symlink_to(tmp_path / "missing")
    (src / "dist" / "linkdir").symlink_to(tmp_path / "outside_dir", target_is_directory=True)
    return src


@pytest.mark.parametrize("patterns", [["*/link"], ["."]], ids=["glob", "walk"])
def test_a_link_to_an_outside_file_is_archived_under_the_links_name(
    linked_tree: Path, tmp_path: Path, patterns: list[str]
) -> None:
    resolved = resolve_paths(patterns, linked_tree)
    assert resolved.root == linked_tree
    assert linked_tree / "dist" / "link" in resolved.files
    build_zip(resolved, tmp_path / "z.zip")
    with zipfile.ZipFile(tmp_path / "z.zip") as zf:
        assert "dist/link" in zf.namelist()
    safe_extract(tmp_path / "z.zip", tmp_path / "o")
    assert (tmp_path / "o" / "dist" / "link").read_bytes() == b"payload"


@pytest.mark.parametrize("patterns", [["dist/*"], ["dist/broken"], ["dist"], ["."]])
def test_a_broken_link_is_dropped(linked_tree: Path, tmp_path: Path, patterns: list[str]) -> None:
    resolved = resolve_paths(patterns, linked_tree)
    assert all(p.name != "broken" for p in resolved.files)
    build_zip(resolved, tmp_path / "z.zip")  # used to raise FileNotFoundError


def test_a_directory_link_inside_a_walked_tree_is_not_descended(linked_tree: Path) -> None:
    assert [p.name for p in resolve_paths(["dist"], linked_tree).files] == ["link", "real.txt"]


def test_a_directory_link_named_by_the_pattern_is_walked(linked_tree: Path, tmp_path: Path) -> None:
    resolved = resolve_paths(["dist/linkdir"], linked_tree)
    assert resolved.files == [linked_tree / "dist" / "linkdir" / "x"]
    assert resolved.root == linked_tree / "dist" / "linkdir"
    build_zip(resolved, tmp_path / "z.zip")
    with zipfile.ZipFile(tmp_path / "z.zip") as zf:
        assert zf.namelist() == ["x"]


def test_a_colon_in_a_posix_file_name_round_trips(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("colons are not legal in Windows file names")
    make_tree(tmp_path / "s", {"build:2026.log": b"log", "sub:dir/x:y": b"xy"})
    build_zip(resolve_paths(["."], tmp_path / "s"), tmp_path / "z.zip")
    safe_extract(tmp_path / "z.zip", tmp_path / "o")
    assert (tmp_path / "o" / "build:2026.log").read_bytes() == b"log"
    assert (tmp_path / "o" / "sub:dir" / "x:y").read_bytes() == b"xy"


@pytest.mark.parametrize(
    ("compression", "causes"),
    [
        (zipfile.ZIP_STORED, (zipfile.BadZipFile,)),
        (zipfile.ZIP_DEFLATED, (zipfile.BadZipFile, zlib.error)),
        (zipfile.ZIP_BZIP2, (zipfile.BadZipFile, OSError, EOFError)),  # bz2 reports OSError
        (zipfile.ZIP_LZMA, (zipfile.BadZipFile, lzma.LZMAError, EOFError)),
    ],
    ids=["stored", "deflated", "bzip2", "lzma"],
)
def test_a_corrupted_member_body_is_an_integrity_error(
    tmp_path: Path, compression: int, causes: tuple[type[Exception], ...]
) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w", compression=compression) as zf:
        zf.writestr("a.bin", bytes(range(256)) * 8)
    raw = bytearray(bad.read_bytes())
    body = raw.index(b"PK\x03\x04") + 30 + len("a.bin")  # local header, name, no extra field
    raw[body + 20] ^= 0xFF  # damage the data; the central directory stays intact
    bad.write_bytes(raw)
    with pytest.raises(IntegrityError, match=r"a\.bin") as caught:
        safe_extract(bad, tmp_path / "o")
    assert isinstance(caught.value.__cause__, causes)


def test_a_truncated_member_is_an_integrity_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.bin", bytes(range(256)) * 8)
    raw = bytearray(bad.read_bytes())
    size_at = raw.index(b"PK\x01\x02") + 20  # compressed size, in the central directory
    (size,) = struct.unpack_from("<I", raw, size_at)
    struct.pack_into("<I", raw, size_at, size // 2)  # the stream now ends mid-way
    bad.write_bytes(raw)
    with pytest.raises(IntegrityError, match=r"a\.bin") as caught:
        safe_extract(bad, tmp_path / "o")
    assert isinstance(caught.value.__cause__, (zipfile.BadZipFile, zlib.error, EOFError))


@pytest.mark.parametrize(
    ("offset", "fmt", "value", "cause"),
    [
        (10, "<H", 99, NotImplementedError),  # a compression method zipfile cannot undo
        (8, "<H", 1, RuntimeError),  # the encrypted flag, with no password to give
    ],
    ids=["unsupported-method", "encrypted"],
)
def test_an_unreadable_member_is_an_integrity_error(
    tmp_path: Path, offset: int, fmt: str, value: int, cause: type[Exception]
) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("a.bin", b"x" * 100)
    raw = bytearray(bad.read_bytes())
    struct.pack_into(fmt, raw, raw.index(b"PK\x01\x02") + offset, value)  # central directory
    bad.write_bytes(raw)
    with pytest.raises(IntegrityError, match=r"a\.bin") as caught:
        safe_extract(bad, tmp_path / "o")
    assert isinstance(caught.value.__cause__, cause)


@pytest.mark.parametrize("header", ["central", "local"])
def test_a_member_name_that_is_not_utf8_is_an_integrity_error(tmp_path: Path, header: str) -> None:
    bad = tmp_path / "bad.zip"
    write_zip(bad, ["ab"])
    raw = bytearray(bad.read_bytes())
    # Set general-purpose bit 11 ("names are UTF-8") and spoil the name, in one of the two headers.
    sig, flags_at, name_at = (
        (b"PK\x01\x02", 8, 46) if header == "central" else (b"PK\x03\x04", 6, 30)
    )
    start = raw.index(sig)
    raw[start + flags_at + 1] |= 0x08
    raw[start + name_at : start + name_at + 2] = b"\xff\xfe"
    bad.write_bytes(raw)
    with pytest.raises(IntegrityError):
        safe_extract(bad, tmp_path / "o")


@pytest.mark.parametrize(
    "names", [["a", "a/b"], ["a/b", "a"], ["a", "a/"], ["a", "./a"], ["d/x", "d//x"]]
)
def test_members_whose_paths_collide_are_an_integrity_error_and_nothing_is_written(
    tmp_path: Path, names: list[str]
) -> None:
    write_zip(tmp_path / "clash.zip", ["ok.txt", *names])
    with pytest.raises(IntegrityError, match="conflict"):
        safe_extract(tmp_path / "clash.zip", tmp_path / "o")
    assert list((tmp_path / "o").rglob("*")) == []


def test_a_repeated_member_name_is_an_integrity_error(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        write_zip(tmp_path / "twice.zip", ["a", "a"])
    with pytest.raises(IntegrityError, match="repeats"):
        safe_extract(tmp_path / "twice.zip", tmp_path / "o")


def test_a_rejected_archive_writes_nothing(tmp_path: Path) -> None:
    write_zip(tmp_path / "bad.zip", ["ok.txt", "sub/ok.txt", "../evil"])
    with pytest.raises(IntegrityError, match="unsafe"):
        safe_extract(tmp_path / "bad.zip", tmp_path / "o")
    assert list((tmp_path / "o").rglob("*")) == []
    assert not (tmp_path / "evil").exists()


def test_a_write_error_is_an_oserror_not_an_integrity_error(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.mkdir()
    (out / "a").write_bytes(b"already here, and a file")
    write_zip(tmp_path / "z.zip", ["a/b"])
    # FileExistsError or NotADirectoryError by platform, so no `match`.
    with pytest.raises(OSError):  # noqa: PT011
        safe_extract(tmp_path / "z.zip", out)


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs POSIX permissions that root ignores",
)
def test_a_permission_error_propagates_as_oserror(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.mkdir()
    out.chmod(0o500)
    write_zip(tmp_path / "z.zip", ["a.txt"])
    try:
        with pytest.raises(PermissionError):
            safe_extract(tmp_path / "z.zip", out)
    finally:
        out.chmod(0o700)


@pytest.mark.skipif(sys.platform == "win32", reason="no executable bit on Windows")
def test_extracted_modes_are_normalised_and_a_symlink_entry_is_only_text(tmp_path: Path) -> None:
    cases = {  # name: (stored mode, extracted permissions)
        "exec": (stat.S_IFREG | 0o777, 0o755),
        "exec-owner-only": (stat.S_IFREG | 0o700, 0o755),
        "untyped-exec": (0o755, 0o755),
        "plain": (stat.S_IFREG | 0o666, 0o644),
        "private": (stat.S_IFREG | 0o600, 0o644),
        "symlink-entry": (stat.S_IFLNK | 0o777, 0o644),
    }
    with zipfile.ZipFile(tmp_path / "m.zip", "w") as zf:
        for name, (mode, _) in cases.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = mode << 16
            zf.writestr(info, b"/etc/passwd")
    safe_extract(tmp_path / "m.zip", tmp_path / "o")
    for name, (_, expected) in cases.items():
        extracted = tmp_path / "o" / name
        assert extracted.is_file()
        assert not extracted.is_symlink()
        assert stat.S_IMODE(extracted.lstat().st_mode) == expected, name


@pytest.mark.parametrize("name", ["a\\b", "C:x", "D:dir/f"])
def test_a_name_that_extraction_would_refuse_is_refused_at_build_time(
    tmp_path: Path, name: str
) -> None:
    if sys.platform == "win32":
        pytest.skip("neither backslashes nor colons are legal in Windows file names")
    make_tree(tmp_path / "s", {name: b"x"})
    resolved = resolve_paths(["."], tmp_path / "s")
    with pytest.raises(UsageError, match="cannot archive") as caught:
        build_zip(resolved, tmp_path / "z.zip")
    assert str(tmp_path / "s" / name) in str(caught.value)
    assert not (tmp_path / "z.zip").exists()


def test_a_pattern_outside_cwd_resolves_to_its_own_root(tmp_path: Path) -> None:
    make_tree(tmp_path, {"elsewhere/dist/a": b"a", "work/x": b"x"})
    r = resolve_paths([str(tmp_path / "elsewhere" / "dist")], tmp_path / "work")
    assert r.files == [tmp_path / "elsewhere" / "dist" / "a"]
    assert r.root == tmp_path / "elsewhere" / "dist"


def test_the_root_does_not_depend_on_which_search_paths_exist(tmp_path: Path) -> None:
    src = tmp_path / "s"
    make_tree(src, {"dist/a.whl": b"a"})
    patterns = ["dist/*.whl", "coverage/**"]
    missing = resolve_paths(patterns, src)
    (src / "coverage").mkdir()
    empty = resolve_paths(patterns, src)
    make_tree(src, {"coverage/index.html": b"h"})
    present = resolve_paths(patterns, src)
    assert missing.root == empty.root == present.root == src
    build_zip(missing, tmp_path / "z.zip")
    with zipfile.ZipFile(tmp_path / "z.zip") as zf:
        assert zf.namelist() == ["dist/a.whl"]


def test_comment_lines_are_ignored(tmp_path: Path) -> None:
    make_tree(tmp_path, {"dist/a.whl": b"a", "#notes": b"n"})
    r = resolve_paths(["# the wheels", "  #notes", "dist/a.whl"], tmp_path)
    assert r.files == [tmp_path / "dist" / "a.whl"]
    assert r.root == tmp_path / "dist"  # a comment contributes no search path either


def test_the_compression_level_is_honoured(tmp_path: Path) -> None:
    text = "".join(f"line {i}: {i * 2654435761 % 9973} of the build log\n" for i in range(40_000))
    assert len(text) > 1_000_000
    make_tree(tmp_path / "s", {"log.txt": text.encode()})
    resolved = resolve_paths(["log.txt"], tmp_path / "s")
    size = {
        level: build_zip(resolved, tmp_path / f"{level}.zip", compression_level=level).size
        for level in (0, 1, 9)
    }
    assert size[9] < size[1] < size[0]
    with zipfile.ZipFile(tmp_path / "0.zip") as zf:
        assert zf.getinfo("log.txt").compress_type == zipfile.ZIP_STORED
    with zipfile.ZipFile(tmp_path / "1.zip") as zf:
        assert zf.getinfo("log.txt").compress_type == zipfile.ZIP_DEFLATED


def test_compression_level_out_of_range_is_refused(tmp_path: Path) -> None:
    make_tree(tmp_path / "s", {"a": b"a"})
    resolved = resolve_paths(["a"], tmp_path / "s")
    with pytest.raises(ValueError, match="compression_level"):
        build_zip(resolved, tmp_path / "z.zip", compression_level=10)


def test_files_over_the_zip64_threshold_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real threshold is 2 GiB; shrink it so a small file crosses it. build_zip streams the
    # file, so it must announce the size up front or zipfile refuses to grow past the limit.
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 100)
    make_tree(tmp_path / "s", {"big": b"0" * 1000})
    res = build_zip(resolve_paths(["big"], tmp_path / "s"), tmp_path / "z.zip")
    assert res.file_count == 1
    safe_extract(tmp_path / "z.zip", tmp_path / "o")
    assert (tmp_path / "o" / "big").read_bytes() == b"0" * 1000


_segment = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), max_codepoint=0x24F),
    min_size=1,
    max_size=8,
)
_relpath = st.lists(_segment, min_size=1, max_size=4).map(lambda parts: "/".join(parts))


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(files=st.dictionaries(_relpath, st.binary(max_size=2048), min_size=1, max_size=12))
def test_round_trip_restores_the_tree(files: dict[str, bytes]) -> None:
    # a path cannot be both a file and a directory prefix of another file
    assume(not any(a != b and b.startswith(a + "/") for a in files for b in files))
    with tempfile.TemporaryDirectory() as td:
        src, out = Path(td) / "src", Path(td) / "out"
        make_tree(src, files)
        r = resolve_paths(["."], src)
        res = build_zip(r, Path(td) / "a.zip")
        assert res.file_count == len(files)
        safe_extract(Path(td) / "a.zip", out)
        for rel, data in files.items():
            assert (out / rel).read_bytes() == data
        assert sorted(
            p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()
        ) == sorted(files)


# ----- edges from the pre-release reviews ------------------------------------------------------


@pytest.mark.parametrize("pattern", ["dist", "dist/", "dist/*", "dist/**", "dist/**/*", "**"])
def test_a_directory_link_reached_through_a_wildcard_is_not_descended(
    linked_tree: Path, pattern: str
) -> None:
    """A wildcard must not do what a walk refuses to: `dist/*` and `**` see exactly the files
    `dist` sees. Only a pattern that names the link itself makes it the top of a walk."""
    files = resolve_paths([pattern], linked_tree).files
    assert [p.relative_to(linked_tree).as_posix() for p in files] == ["dist/link", "dist/real.txt"]


def test_a_cyclic_directory_link_under_a_recursive_wildcard_is_archived_once(
    linked_tree: Path, tmp_path: Path
) -> None:
    """Python 3.11 and 3.12 follow directory links under `**`, 3.13 does not; a link back to an
    ancestor used to put dozens of copies of the tree into the archive on the older two."""
    make_tree(linked_tree, {"dist/sub/deep.txt": b"deep"})
    (linked_tree / "dist" / "sub" / "loop").symlink_to(
        linked_tree / "dist", target_is_directory=True
    )
    for pattern in ("dist/**", "**/*.txt", "dist/**/deep.txt"):
        names = [
            p.relative_to(linked_tree).as_posix()
            for p in resolve_paths([pattern], linked_tree).files
        ]
        assert all("loop" not in name for name in names), pattern
        assert "dist/sub/deep.txt" in names, pattern
    resolved = resolve_paths(["dist/**"], linked_tree)
    build_zip(resolved, tmp_path / "z.zip")
    with zipfile.ZipFile(tmp_path / "z.zip") as zf:
        assert zf.namelist() == ["link", "real.txt", "sub/deep.txt"]


def test_a_file_link_reached_through_a_wildcard_is_still_archived(linked_tree: Path) -> None:
    assert linked_tree / "dist" / "link" in resolve_paths(["dist/l*"], linked_tree).files


def test_a_pattern_that_climbs_out_of_its_own_root_is_a_usage_error_at_build_time(
    tmp_path: Path,
) -> None:
    make_tree(tmp_path, {"a/s/x": b"x", "b": b"b"})
    resolved = resolve_paths(["a/*/../../b"], tmp_path)
    with pytest.raises(UsageError, match=r"cannot archive .*outside") as caught:
        build_zip(resolved, tmp_path / "z.zip")
    assert str(tmp_path / "b") in str(caught.value)
    assert not (tmp_path / "z.zip").exists()


def test_patterns_without_a_common_root_are_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, `C:\\out\\*` and `D:\\out\\*` share no directory; `commonpath` raises."""
    make_tree(tmp_path, {"a/x": b"x", "b/y": b"y"})

    def two_drives(_paths: list[str]) -> str:
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(os.path, "commonpath", two_drives)
    with pytest.raises(UsageError, match="no common directory"):
        resolve_paths(["a", "b"], tmp_path)


def test_an_empty_directory_with_a_file_below_it_elsewhere_is_not_recorded_twice(
    tmp_path: Path,
) -> None:
    """`empty` holds only directories no archived file lies under, however the two were matched."""
    make_tree(tmp_path, {"a/keep/f": b"f", "a/other/g.tmp": b"g"}, dirs=("a/void",))
    resolved = resolve_paths(["a", "!a/**/*.tmp"], tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in resolved.files] == ["a/keep/f"]
    assert [p.relative_to(tmp_path).as_posix() for p in resolved.empty_dirs] == ["a/void"]
