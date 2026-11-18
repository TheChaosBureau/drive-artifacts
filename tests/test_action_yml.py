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


def _run_upload_step(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run the upload step's `run:` block as Actions does; the stub `uvx` prints its argv."""
    assert BASH is not None
    (step,) = (s for s in _load("action.yml")["runs"]["steps"] if s.get("id") == "upload")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uvx = bin_dir / "uvx"
    uvx.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    uvx.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DA_SOURCE": SOURCE,
        "DA_NAME": "artifact",
        "DA_PATH": "dist/",
        "DA_RETENTION_DAYS": "90",
        "DA_IF_NO_FILES_FOUND": "warn",
        "DA_COMPRESSION_LEVEL": "6",
        "DA_OVERWRITE": "false",
        "DA_INCLUDE_HIDDEN": "false",
        "DA_PRUNE": "true",
        **overrides,
    }
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


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
    assert argv[:4] == ["--from", SOURCE, "drive-artifacts", "upload"]
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
