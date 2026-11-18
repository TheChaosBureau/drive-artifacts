# SPDX-License-Identifier: Apache-2.0
import hashlib
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from drive_artifacts.errors import UsageError
from drive_artifacts.model import (
    FOLDER_MIME,
    MAX_NAME_BYTES,
    PROPERTY_LIMIT,
    REF_MAX_BYTES,
    ArtifactKey,
    ArtifactRecord,
    artifact_properties,
    check_properties,
    escape,
    fit_ref,
    folder_properties,
    prop,
    query,
    validate_artifact_name,
)

KEY = ArtifactKey(repo_id="123", run_id="456", run_attempt="1", name="wheel")


@pytest.mark.parametrize(
    "name",
    ["wheel", "my artifact", "a.b-c_d", "ünïcode", "x" * MAX_NAME_BYTES, "...", ".hidden", "a.."],
)
def test_valid_names_pass_through(name: str) -> None:
    assert validate_artifact_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        'a"b',
        "a:b",
        "a<b",
        "a>b",
        "a|b",
        "a*b",
        "a?b",
        "a\rb",
        "a\nb",
        "a/b",
        "a\\b",
        "ev\x00il",  # no filesystem path may hold a NUL, and the name becomes one
        "x" * (MAX_NAME_BYTES + 1),
        "é" * 60,
        # path steps, not names: download-all joins the name onto --path
        ".",
        "..",
        " .. ",
    ],
)
def test_invalid_names_raise_usage_error(name: str) -> None:
    with pytest.raises(UsageError):
        validate_artifact_name(name)


def test_name_is_stripped() -> None:
    assert validate_artifact_name("  wheel ") == "wheel"


def test_filename_layout() -> None:
    assert KEY.filename == "456-1-wheel.zip"


def _props() -> dict[str, str]:
    return artifact_properties(
        KEY, sha256="ab" * 32, ref="refs/heads/main", commit="c" * 40, expires=date(2026, 12, 31)
    )


def test_artifact_properties_round_trip_through_a_drive_file() -> None:
    props = _props()
    assert props == {
        "da.v": "1",
        "da.kind": "artifact",
        "da.repo": "123",
        "da.run": "456",
        "da.attempt": "1",
        "da.name": "wheel",
        "da.sha256": "ab" * 32,
        "da.ref": "refs/heads/main",
        "da.commit": "c" * 40,
        "da.expires": "2026-12-31",
    }
    file = {
        "id": "fid",
        "name": KEY.filename,
        "size": "42",
        "appProperties": props,
        "webViewLink": "https://drive.google.com/file/d/fid/view",
        "createdTime": "2026-10-10T00:00:00.000Z",
        "modifiedTime": "2026-10-11T00:00:00.000Z",
    }
    rec = ArtifactRecord.from_drive(file)
    assert rec.key == KEY
    assert (rec.file_id, rec.size, rec.sha256) == ("fid", 42, "ab" * 32)
    assert (rec.ref, rec.commit) == ("refs/heads/main", "c" * 40)
    assert rec.expires == date(2026, 12, 31)
    assert (rec.web_view_link, rec.modified_time) == (
        "https://drive.google.com/file/d/fid/view",
        "2026-10-11T00:00:00.000Z",
    )
    assert rec.to_json() == {
        "name": "wheel",
        "run_id": "456",
        "run_attempt": "1",
        "repo_id": "123",
        "file_id": "fid",
        "sha256": "ab" * 32,
        "size": 42,
        "ref": "refs/heads/main",
        "commit": "c" * 40,
        "expires": "2026-12-31",
        "url": "https://drive.google.com/file/d/fid/view",
        "modified": "2026-10-11T00:00:00.000Z",
        "created": "2026-10-10T00:00:00.000Z",
    }


def test_from_drive_defaults_for_a_minimal_file() -> None:
    props = _props()
    for optional in ("da.ref", "da.commit"):
        del props[optional]
    rec = ArtifactRecord.from_drive({"id": "fid", "appProperties": props})
    assert rec.size == 0
    assert (rec.ref, rec.commit) == ("", "")
    assert (rec.web_view_link, rec.modified_time, rec.created_time) == ("", "", "")
    assert rec.to_json()["size"] == 0
    assert rec.to_json()["url"] == ""


@pytest.mark.parametrize(
    "file",
    [
        pytest.param({"id": "x", "name": "y", "appProperties": {}}, id="empty-properties"),
        pytest.param({"id": "x", "name": "y"}, id="no-appProperties-key"),
        pytest.param({"id": "x", "appProperties": "oops"}, id="properties-not-a-mapping"),
        pytest.param({"id": "x", "appProperties": folder_properties("root")}, id="other-kind"),
        pytest.param({"id": "x", "appProperties": {**_props(), "da.v": "2"}}, id="other-version"),
        pytest.param(
            {"id": "x", "appProperties": {k: v for k, v in _props().items() if k != "da.v"}},
            id="no-version",
        ),
    ],
)
def test_from_drive_rejects_foreign_files(file: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="not a drive-artifacts file: 'x'"):
        ArtifactRecord.from_drive(file)


@pytest.mark.parametrize(
    ("overrides", "dropped", "extra"),
    [
        pytest.param({}, "da.repo", {}, id="missing-property"),
        pytest.param({}, "da.sha256", {}, id="missing-digest"),
        pytest.param({"da.expires": "soon"}, None, {}, id="bad-date"),
        pytest.param({}, None, {"size": "big"}, id="bad-size"),
    ],
)
def test_from_drive_wraps_corrupt_files_in_a_value_error_naming_the_file(
    overrides: dict[str, str], dropped: str | None, extra: dict[str, object]
) -> None:
    props = {**_props(), **overrides}
    if dropped is not None:
        del props[dropped]
    with pytest.raises(ValueError, match="malformed drive-artifacts file 'fid'"):
        ArtifactRecord.from_drive({"id": "fid", "appProperties": props, **extra})


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("..", id="parent"),
        pytest.param(".", id="self"),
        pytest.param("../x", id="relative-escape"),
        pytest.param("/etc/cron.d", id="absolute"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("ev\x00il", id="nul"),
        pytest.param("", id="empty"),
        pytest.param(" wheel", id="not-canonical-leading-space"),
        pytest.param("wheel ", id="not-canonical-trailing-space"),
        pytest.param("x" * (MAX_NAME_BYTES + 1), id="too-long"),
    ],
)
def test_from_drive_refuses_a_planted_name_that_put_could_not_have_stored(planted: str) -> None:
    """The name is a directory on download-all, and anyone able to write properties can set it."""
    with pytest.raises(ValueError, match="malformed drive-artifacts file 'fid'"):
        ArtifactRecord.from_drive({"id": "fid", "appProperties": {**_props(), "da.name": planted}})


def test_from_drive_corrupt_file_without_an_id_is_still_a_value_error() -> None:
    with pytest.raises(ValueError, match="malformed drive-artifacts file None"):
        ArtifactRecord.from_drive({"appProperties": _props()})


def test_one_corrupt_file_does_not_abort_a_listing() -> None:
    files: list[dict[str, object]] = [
        {"id": "a", "appProperties": _props()},
        {"id": "b", "appProperties": {**_props(), "da.expires": "soon"}},
        {"id": "c", "appProperties": folder_properties("root")},
        {"id": "d", "appProperties": _props()},
    ]
    kept: list[str] = []
    for file in files:
        try:
            kept.append(ArtifactRecord.from_drive(file).file_id)
        except ValueError:
            continue
    assert kept == ["a", "d"]


def test_folder_properties() -> None:
    assert folder_properties("root") == {"da.v": "1", "da.kind": "root"}
    assert folder_properties("repo", "123") == {"da.v": "1", "da.kind": "repo", "da.repo": "123"}


def test_repo_folder_without_a_repo_id_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="repo id"):
        folder_properties("repo")


def test_check_properties_enforces_the_124_byte_limit() -> None:
    ok = {"da.name": "x" * (PROPERTY_LIMIT - len("da.name"))}
    assert check_properties(ok) == ok
    with pytest.raises(UsageError, match="124"):
        check_properties({"da.name": "x" * (PROPERTY_LIMIT - len("da.name") + 1)})


LONE_SURROGATE = "\udcff"  # what argv/env bytes that are not UTF-8 decode to on POSIX


def test_non_utf8_artifact_name_is_a_usage_error_not_a_unicode_error() -> None:
    with pytest.raises(UsageError, match="artifact name is not valid UTF-8"):
        validate_artifact_name(LONE_SURROGATE)
    with pytest.raises(UsageError, match="artifact name is not valid UTF-8"):
        validate_artifact_name(f"wheel{LONE_SURROGATE}")


@pytest.mark.parametrize(
    "props",
    [
        pytest.param({"da.name": LONE_SURROGATE}, id="value"),
        pytest.param({LONE_SURROGATE: "x"}, id="key"),
    ],
)
def test_non_utf8_property_is_a_usage_error_not_a_unicode_error(props: dict[str, str]) -> None:
    with pytest.raises(UsageError, match="not valid UTF-8"):
        check_properties(props)


def test_non_utf8_ref_is_a_usage_error_not_a_unicode_error() -> None:
    with pytest.raises(UsageError, match="ref is not valid UTF-8"):
        fit_ref(LONE_SURROGATE)
    with pytest.raises(UsageError, match="ref is not valid UTF-8"):
        artifact_properties(
            KEY, sha256="ab" * 32, ref=LONE_SURROGATE, commit="", expires=date(2026, 12, 31)
        )


@pytest.mark.parametrize(
    "ref",
    ["", "refs/heads/main", "refs/tags/v1.2.3", "x" * REF_MAX_BYTES, "é" * 58],
)
def test_short_refs_pass_through_fit_ref_unchanged(ref: str) -> None:
    assert fit_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    ["refs/heads/renovate/" + "x" * 180, "é" * 60],  # 200 bytes; 120 bytes in 60 characters
)
def test_long_refs_become_a_47_character_digest_and_do_not_fail_the_upload(ref: str) -> None:
    assert len(ref.encode()) > REF_MAX_BYTES
    out = fit_ref(ref)
    assert out == "sha256:" + hashlib.sha256(ref.encode()).hexdigest()[:40]
    assert len(out) == 47
    props = artifact_properties(
        KEY, sha256="ab" * 32, ref=ref, commit="c" * 40, expires=date(2026, 12, 31)
    )
    assert props["da.ref"] == out
    assert len("da.ref") + len(props["da.ref"]) <= PROPERTY_LIMIT
    rec = ArtifactRecord.from_drive({"id": "fid", "appProperties": props})
    assert rec.ref == out


def test_fit_ref_is_deterministic_and_distinguishes_refs() -> None:
    a = "refs/heads/renovate/" + "a" * 150
    b = "refs/heads/renovate/" + "b" * 150
    assert fit_ref(a) == fit_ref(a)
    assert fit_ref(a) != fit_ref(b)


@given(st.text(max_size=300))
def test_fit_ref_always_fits_a_property_and_is_stable(ref: str) -> None:
    out = fit_ref(ref)
    assert len("da.ref") + len(out.encode()) <= PROPERTY_LIMIT
    assert fit_ref(out) == out
    assert out == ref or len(ref.encode()) > REF_MAX_BYTES


def test_escape_and_query() -> None:
    assert escape("quinn's paper\\essay") == "quinn\\'s paper\\\\essay"
    assert prop("da.repo", "123") == "appProperties has { key='da.repo' and value='123' }"
    assert query(prop("da.repo", "1"), f"mimeType = '{FOLDER_MIME}'") == (
        "appProperties has { key='da.repo' and value='1' } and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )


@given(st.text(min_size=1, max_size=200))
def test_validate_never_returns_an_oversized_or_forbidden_name(name: str) -> None:
    try:
        out = validate_artifact_name(name)
    except UsageError:
        return
    assert len(out.encode()) <= MAX_NAME_BYTES
    assert not any(c in out for c in '":<>|*?\r\n/\\\x00')
    assert out == out.strip()
    assert out not in {".", ".."}


def _unescape(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\":
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


@given(st.text(max_size=100))
def test_escape_is_reversible(value: str) -> None:
    out = escape(value)
    assert _unescape(out) == value
    assert out.count("\\") == 2 * value.count("\\") + value.count("'")
