# SPDX-License-Identifier: Apache-2.0
"""Reading the GitHub Actions environment and writing its files and annotations."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from drive_artifacts.errors import UsageError
from drive_artifacts.model import Repo, RunContext


def in_actions(env: Mapping[str, str]) -> bool:
    return env.get("GITHUB_ACTIONS") == "true"


def _pick(explicit: str | None, env: Mapping[str, str], var: str) -> str | None:
    if explicit is not None and explicit != "":
        return explicit
    value = env.get(var, "")
    return value or None


def read_run_context(
    env: Mapping[str, str],
    *,
    repo_id: str | None = None,
    repo: str | None = None,
    run_id: str | None = None,
    run_attempt: str | None = None,
    ref: str | None = None,
    commit: str | None = None,
    require_run: bool = True,
) -> RunContext:
    """Build the run context. Explicit values win; the `GITHUB_*` variables fill the rest."""
    rid = _pick(repo_id, env, "GITHUB_REPOSITORY_ID")
    slug = _pick(repo, env, "GITHUB_REPOSITORY")
    if rid is None or slug is None:
        missing = [opt for opt, value in (("--repo-id", rid), ("--repo", slug)) if value is None]
        raise UsageError(f"not inside GitHub Actions: pass {' and '.join(missing)}")
    run = _pick(run_id, env, "GITHUB_RUN_ID")
    if run is None and require_run:
        raise UsageError("not inside GitHub Actions: pass --run-id")
    return RunContext(
        repo=Repo(id=rid, slug=slug),
        run_id=run or "",
        run_attempt=_pick(run_attempt, env, "GITHUB_RUN_ATTEMPT") or "1",
        ref=_pick(ref, env, "GITHUB_REF") or "",
        commit=_pick(commit, env, "GITHUB_SHA") or "",
    )


def _encode(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class Actions:
    """Outputs, step summary, masks and annotations. Inert outside Actions."""

    def __init__(self, env: Mapping[str, str], stderr: TextIO) -> None:
        self._active = in_actions(env)
        self._output = (
            Path(env["GITHUB_OUTPUT"]) if self._active and env.get("GITHUB_OUTPUT") else None
        )
        self._summary = (
            Path(env["GITHUB_STEP_SUMMARY"])
            if self._active and env.get("GITHUB_STEP_SUMMARY")
            else None
        )
        self._err = stderr

    def output(self, name: str, value: str) -> None:
        if self._output is None:
            return
        self._output.parent.mkdir(parents=True, exist_ok=True)
        with self._output.open("a", encoding="utf-8") as fh:
            # A lone carriage return would end the line for some readers too: frame it.
            if "\n" in value or "\r" in value:
                delimiter = f"da_{secrets.token_hex(8)}"
                fh.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                fh.write(f"{name}={value}\n")

    def summary(self, markdown: str) -> None:
        if self._summary is None:
            return
        self._summary.parent.mkdir(parents=True, exist_ok=True)
        with self._summary.open("a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip("\n") + "\n")

    def mask(self, value: str) -> None:
        if self._active and value:
            self._err.write(f"::add-mask::{_encode(value)}\n")

    def _annotate(self, level: str, message: str) -> None:
        if self._active:
            self._err.write(f"::{level}::{_encode(message)}\n")
        else:
            self._err.write(f"{level}: {message}\n")

    def error(self, message: str) -> None:
        self._annotate("error", message)

    def warning(self, message: str) -> None:
        self._annotate("warning", message)

    def notice(self, message: str) -> None:
        self._annotate("notice", message)
