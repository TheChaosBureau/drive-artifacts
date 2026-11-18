# SPDX-License-Identifier: Apache-2.0
import io
import os
from pathlib import Path

import pytest

from drive_artifacts.errors import UsageError
from drive_artifacts.github import Actions, in_actions, read_run_context

ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "TheChaosBureau/drive-artifacts",
    "GITHUB_REPOSITORY_ID": "987",
    "GITHUB_RUN_ID": "12345",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_SHA": "a" * 40,
}


def test_in_actions() -> None:
    assert in_actions(ENV)
    assert not in_actions({})
    assert not in_actions({"GITHUB_ACTIONS": "false"})


def test_run_context_from_env() -> None:
    ctx = read_run_context(ENV)
    assert (ctx.repo.id, ctx.repo.slug) == ("987", "TheChaosBureau/drive-artifacts")
    assert (ctx.run_id, ctx.run_attempt, ctx.ref, ctx.commit) == (
        "12345",
        "2",
        "refs/heads/main",
        "a" * 40,
    )


def test_explicit_arguments_win() -> None:
    ctx = read_run_context(
        ENV,
        run_id="1",
        run_attempt="9",
        ref="refs/tags/v1",
        commit="b" * 40,
        repo_id="1",
        repo="o/r",
    )
    assert (ctx.repo.id, ctx.repo.slug, ctx.run_id, ctx.run_attempt) == ("1", "o/r", "1", "9")
    assert (ctx.ref, ctx.commit) == ("refs/tags/v1", "b" * 40)


def test_outside_actions_defaults_are_sane_but_repo_and_run_are_required() -> None:
    with pytest.raises(UsageError, match="--repo-id"):
        read_run_context({})
    with pytest.raises(UsageError, match="--run-id"):
        read_run_context({}, repo_id="1", repo="o/r")
    ctx = read_run_context({}, repo_id="1", repo="o/r", run_id="5")
    assert (ctx.run_attempt, ctx.ref, ctx.commit) == ("1", "", "")
    assert read_run_context({}, repo_id="1", repo="o/r", require_run=False).run_id == ""


def test_outputs_and_summary_are_appended_to_the_github_files(tmp_path: Path) -> None:
    # the directory is created on demand
    out, summary = tmp_path / "missing-dir" / "out", tmp_path / "summary"
    env = {**ENV, "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)}
    err = io.StringIO()
    a = Actions(env, err)
    a.output("artifact-id", "abc")
    a.output("multi", "line1\nline2")
    a.summary("hello **world**")
    a.mask("s3cr3t")
    a.error("boom")
    a.warning("careful")
    a.notice("fyi")
    text = out.read_text()
    assert "artifact-id=abc\n" in text
    assert "multi<<" in text
    assert "line1\nline2\n" in text
    assert summary.read_text() == "hello **world**\n"
    lines = err.getvalue().splitlines()
    assert "::add-mask::s3cr3t" in lines
    assert "::error::boom" in lines
    assert "::warning::careful" in lines
    assert "::notice::fyi" in lines


def test_outside_actions_annotations_are_plain_and_files_untouched(tmp_path: Path) -> None:
    err = io.StringIO()
    a = Actions({"GITHUB_OUTPUT": str(tmp_path / "out")}, err)
    a.output("x", "y")
    a.summary("z")
    a.mask("tok")
    a.error("boom")
    a.warning("w")
    a.notice("n")
    assert not (tmp_path / "out").exists()
    assert err.getvalue().splitlines() == ["error: boom", "warning: w", "notice: n"]


def test_annotation_newlines_are_encoded() -> None:
    err = io.StringIO()
    Actions(ENV, err).error("two\nlines")
    assert err.getvalue() == "::error::two%0Alines\n"


def test_mask_encodes_the_value_like_any_command_data() -> None:
    """The runner unescapes %25, %0D and %0A in command data, so a raw `%0A` in a secret would
    register a different string as the mask."""
    err = io.StringIO()
    Actions(ENV, err).mask("a%0Ab\r\nc")
    assert err.getvalue() == "::add-mask::a%250Ab%0D%0Ac\n"


def test_mask_of_an_empty_value_writes_nothing() -> None:
    err = io.StringIO()
    Actions(ENV, err).mask("")
    assert err.getvalue() == ""


def test_an_output_with_a_carriage_return_uses_the_heredoc_form(tmp_path: Path) -> None:
    out = tmp_path / "out"
    Actions({**ENV, "GITHUB_OUTPUT": str(out)}, io.StringIO()).output("x", "a\rb")
    # Bytes, not text: reading as text would fold the lone carriage return into a line end.
    # The file is written in text mode, so line ends are the platform's (CRLF on Windows).
    text = out.read_bytes().decode()
    eol = os.linesep
    name, delimiter = text.split(eol, 1)[0].split("<<")
    assert name == "x"
    assert text == f"x<<{delimiter}{eol}a\rb{eol}{delimiter}{eol}"


def test_a_multi_line_output_is_closed_by_its_own_delimiter(tmp_path: Path) -> None:
    out = tmp_path / "out"
    Actions({**ENV, "GITHUB_OUTPUT": str(out)}, io.StringIO()).output("multi", "line1\nline2")
    lines = out.read_text().split("\n")
    delimiter = lines[0].removeprefix("multi<<")
    assert delimiter.startswith("da_")
    assert len(delimiter) == 19
    assert lines == [f"multi<<{delimiter}", "line1", "line2", delimiter, ""]


def test_annotations_encode_percent_and_carriage_return() -> None:
    err = io.StringIO()
    Actions(ENV, err).warning("100%\r\ndone")
    assert err.getvalue() == "::warning::100%25%0D%0Adone\n"


def test_in_actions_without_an_output_file_outputs_are_dropped_quietly() -> None:
    err = io.StringIO()
    a = Actions(ENV, err)  # GITHUB_ACTIONS=true but no GITHUB_OUTPUT or GITHUB_STEP_SUMMARY
    a.output("x", "y")
    a.summary("z")
    a.notice("still annotates")
    assert err.getvalue() == "::notice::still annotates\n"


def test_the_usage_error_names_only_the_missing_option() -> None:
    with pytest.raises(UsageError) as only_repo:
        read_run_context({}, repo_id="1")
    assert "--repo" in str(only_repo.value)
    assert "--repo-id" not in str(only_repo.value)
    with pytest.raises(UsageError) as only_id:
        read_run_context({}, repo="o/r")
    assert "--repo-id" in str(only_id.value)
    assert "--repo " not in str(only_id.value) + " "
    with pytest.raises(UsageError, match="--repo-id and --repo"):
        read_run_context({})
