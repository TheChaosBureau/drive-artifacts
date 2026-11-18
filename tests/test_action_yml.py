# SPDX-License-Identifier: Apache-2.0
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from drive_artifacts import __version__

ROOT = Path(__file__).resolve().parents[1]
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40} # v\d+\.\d+\.\d+$")


def _load(rel: str) -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((ROOT / rel).read_text())  # type: ignore[no-any-return]


def test_upload_action_contract() -> None:
    a = _load("action.yml")
    assert a["runs"]["using"] == "composite"
    assert set(a["inputs"]) == {
        "name",
        "path",
        "token",
        "retention-days",
        "if-no-files-found",
        "compression-level",
        "overwrite",
        "include-hidden-files",
        "root",
        "prune",
        "source",
    }
    assert set(a["outputs"]) == {"artifact-id", "artifact-url", "artifact-digest", "size"}
    assert a["inputs"]["token"]["required"] is True
    assert a["inputs"]["path"]["required"] is True
    assert a["inputs"]["source"]["default"] == f"drive-artifacts=={__version__}"


def test_download_action_contract() -> None:
    a = _load("download/action.yml")
    assert a["runs"]["using"] == "composite"
    assert set(a["inputs"]) == {
        "name",
        "path",
        "token",
        "run-id",
        "ref",
        "commit",
        "root",
        "source",
    }
    assert set(a["outputs"]) == {"download-path", "artifact-digest"}
    assert a["inputs"]["source"]["default"] == f"drive-artifacts=={__version__}"


def test_every_uses_is_sha_pinned_and_no_input_reaches_a_run_block_unescaped() -> None:
    for rel in ("action.yml", "download/action.yml"):
        text = (ROOT / rel).read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("- uses:", "uses:")):
                assert PINNED.match(stripped.split("uses:", 1)[1].strip()), (
                    f"{rel}: unpinned {stripped}"
                )
        for step in _load(rel)["runs"]["steps"]:
            if "run" in step:
                assert "${{" not in step["run"], f"{rel}: expression inside run: {step['run']}"


# ----- the upload step's shell, run for real with a stand-in for uvx ---------------------------

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None,
    reason="runs the step under bash with a POSIX shell stand-in for uvx",
)
SOURCE = "drive-artifacts==0.0.0"


UPLOAD_ENV = {
    "DA_NAME": "artifact",
    "DA_PATH": "dist/",
    "DA_RETENTION_DAYS": "90",
    "DA_IF_NO_FILES_FOUND": "warn",
    "DA_COMPRESSION_LEVEL": "6",
    "DA_OVERWRITE": "false",
    "DA_INCLUDE_HIDDEN": "false",
    "DA_PRUNE": "true",
}
DOWNLOAD_ENV = {"DA_NAME": "", "DA_PATH": ".", "DA_RUN_ID": "", "DA_REF": "", "DA_COMMIT": ""}


def _run_step(
    tmp_path: Path, rel: str, step_id: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run one step's `run:` block as Actions does; the stub `uvx` prints its argv."""
    assert BASH is not None
    (step,) = (s for s in _load(rel)["runs"]["steps"] if s.get("id") == step_id)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uvx = bin_dir / "uvx"
    uvx.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    uvx.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DA_SOURCE": SOURCE, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _run_upload_step(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    return _run_step(tmp_path, "action.yml", "upload", {**UPLOAD_ENV, **overrides})


def _run_download_step(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    return _run_step(tmp_path, "download/action.yml", "download", {**DOWNLOAD_ENV, **overrides})


CLI = ["--no-config", "--from", SOURCE, "drive-artifacts"]


@needs_bash
@pytest.mark.parametrize(
    ("variable", "value", "flag", "passed"),
    [
        ("DA_PRUNE", "true", "--no-prune", False),
        ("DA_PRUNE", "True", "--no-prune", False),
        ("DA_PRUNE", "TRUE", "--no-prune", False),
        ("DA_PRUNE", "false", "--no-prune", True),
        ("DA_PRUNE", "False", "--no-prune", True),
        ("DA_PRUNE", "FALSE", "--no-prune", True),
        ("DA_OVERWRITE", "true", "--overwrite", True),
        ("DA_OVERWRITE", "True", "--overwrite", True),
        ("DA_OVERWRITE", "false", "--overwrite", False),
        ("DA_OVERWRITE", "False", "--overwrite", False),
        ("DA_INCLUDE_HIDDEN", "TRUE", "--include-hidden-files", True),
        ("DA_INCLUDE_HIDDEN", "False", "--include-hidden-files", False),
    ],
)
def test_boolean_inputs_take_every_yaml_spelling_of_true_and_false(
    tmp_path: Path, variable: str, value: str, flag: str, passed: bool
) -> None:
    result = _run_upload_step(tmp_path, **{variable: value})
    assert result.returncode == 0, result.stdout + result.stderr
    argv = result.stdout.splitlines()
    assert argv[:5] == [*CLI, "upload"]
    assert (flag in argv) is passed


@needs_bash
@pytest.mark.parametrize(
    ("variable", "name"),
    [
        ("DA_PRUNE", "prune"),
        ("DA_OVERWRITE", "overwrite"),
        ("DA_INCLUDE_HIDDEN", "include-hidden-files"),
    ],
)
@pytest.mark.parametrize("value", ["yes", "1", ""])
def test_a_boolean_input_that_is_neither_true_nor_false_fails_the_step(
    tmp_path: Path, variable: str, name: str, value: str
) -> None:
    result = _run_upload_step(tmp_path, **{variable: value})
    assert result.returncode != 0
    assert f"::error::{name} must be true or false, got '{value}'" in result.stdout
    assert "--from" not in result.stdout  # the CLI never ran


@needs_bash
def test_the_upload_step_ignores_the_callers_uv_config_and_binds_values_to_their_options(
    tmp_path: Path,
) -> None:
    """A name or path that starts with a dash must stay a value, and a consumer repository's
    `uv.toml` must not change how the action installs itself."""
    result = _run_upload_step(tmp_path, DA_NAME="-x", DA_PATH="-rf\ndist/")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("\n".join([*CLI, "upload", "--name=-x", "--path=-rf", "dist/"]))


@needs_bash
def test_the_download_step_passes_only_the_selectors_that_were_given(tmp_path: Path) -> None:
    result = _run_download_step(tmp_path, DA_PATH="-out")
    assert result.stdout.splitlines() == [*CLI, "download", "--path=-out"]
    result = _run_download_step(
        tmp_path, DA_NAME="-n", DA_RUN_ID="7", DA_REF="refs/heads/main", DA_COMMIT="abc"
    )
    assert result.stdout.splitlines() == [
        *CLI,
        "download",
        "--path=.",
        "--name=-n",
        "--run-id=7",
        "--ref=refs/heads/main",
        "--commit=abc",
    ]


@needs_bash
def test_a_bad_boolean_is_reported_as_one_escaped_workflow_command(tmp_path: Path) -> None:
    result = _run_upload_step(tmp_path, DA_PRUNE="50%\n::set-output name=x::y")
    assert result.returncode != 0
    assert result.stdout.splitlines() == [
        "::error::prune must be true or false, got '50%25%0A::set-output name=x::y'"
    ]


def _steps(rel: str) -> list[dict]:  # type: ignore[type-arg]
    return _load(rel)["runs"]["steps"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("rel", ["action.yml", "download/action.yml"])
def test_action_steps_are_pinned_shelled_and_wired_to_real_step_ids(rel: str) -> None:
    action = _load(rel)
    steps = _steps(rel)
    ids = {step["id"] for step in steps if "id" in step}
    for step in steps:
        if "uses" in step:
            ref, _, version = step["uses"].partition("@")
            assert re.fullmatch(r"[\w.-]+/[\w.-]+(?:/[\w.-]+)*", ref), f"{rel}: {step['uses']}"
            assert re.fullmatch(r"[0-9a-f]{40}", version), f"{rel}: unpinned {step['uses']}"
        if "run" in step:
            assert step.get("shell") == "bash", f"{rel}: a run step needs an explicit shell"
            assert step["env"]["PYTHONUTF8"] == "1", f"{rel}: Windows consoles are not UTF-8"
    for name, output in action["outputs"].items():
        match = re.fullmatch(r"\$\{\{ steps\.([\w-]+)\.outputs\.([\w-]+) \}\}", output["value"])
        assert match, f"{rel}: output {name} is not a step output"
        assert match.group(1) in ids, f"{rel}: output {name} reads a step that does not exist"
        assert match.group(2) == name


def test_both_actions_pin_the_same_setup_uv() -> None:
    pins = {
        step["uses"]
        for rel in ("action.yml", "download/action.yml")
        for step in _steps(rel)
        if "uses" in step
    }
    assert len(pins) == 1, pins


def test_input_defaults_match_the_documented_ones() -> None:
    upload = {k: v.get("default") for k, v in _load("action.yml")["inputs"].items()}
    assert upload == {
        "name": "artifact",
        "path": None,
        "token": None,
        "retention-days": "90",
        "if-no-files-found": "warn",
        "compression-level": "6",
        "overwrite": "false",
        "include-hidden-files": "false",
        "root": "GitHub Artifacts",
        "prune": "true",
        "source": f"drive-artifacts=={__version__}",
    }
    download = {k: v.get("default") for k, v in _load("download/action.yml")["inputs"].items()}
    assert download == {
        "name": "",
        "path": ".",
        "token": None,
        "run-id": "",
        "ref": "",
        "commit": "",
        "root": "GitHub Artifacts",
        "source": f"drive-artifacts=={__version__}",
    }


def test_the_e2e_workflow_hands_the_client_secret_to_the_action_steps_only() -> None:
    workflow = _load(".github/workflows/e2e.yml")
    for name, job in workflow["jobs"].items():
        assert "DRIVE_ARTIFACTS_CLIENT_SECRET" not in job.get("env", {}), name
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("./"):
                assert "DRIVE_ARTIFACTS_CLIENT_SECRET" in step["env"], name
            else:
                assert "DRIVE_ARTIFACTS_CLIENT_SECRET" not in step.get("env", {}), name
