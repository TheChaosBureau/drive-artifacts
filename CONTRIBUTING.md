# Contributing

- `uv sync --all-groups && uv run pre-commit install`
- Tests first. Every change to `src/` comes with tests under `tests/` and a line
  in `CHANGELOG.md` under *Unreleased*; CI checks the changelog. A change that
  needs no entry gets the `skip-changelog` label, and adding or removing the
  label re-runs the check.
- `ruff`, `mypy --strict`, `pyright`, `zizmor` and `actionlint` must be clean;
  coverage must stay at or above 95 %.
- Workflows pin actions by full SHA with a `# vX.Y.Z` comment, and pass inputs to
  `run:` only through `env:`.
- Keep the Drive surface small. New Drive calls go in `drive.py` and get a fake
  implementation in `tests/fake_drive.py` first.
