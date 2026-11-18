# SPDX-License-Identifier: Apache-2.0
import io
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
