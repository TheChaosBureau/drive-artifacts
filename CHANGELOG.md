# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `download` without `--name` on a run that stored nothing now succeeds with a warning and
  still sets `download-path`, as `actions/download-artifact` does. A download that names an
  artifact still exits 4 when it is missing.
- Both actions run the CLI with `uvx --no-config`, so the calling repository's `uv.toml`
  cannot change how the action installs itself, and pass every input as `--option=value`,
  so a name or path that starts with a dash stays a value.

### Fixed

- `download --path ~/dir` expands the tilde.
- `download --ref` and `--commit` ask Drive for one page of results however many artifacts
  the repository holds.
- An upload whose size could not be verified because the re-read of the file failed is
  trashed, like one whose size is wrong, so a plain retry does not conflict with it.
- `list | head` ends quietly (exit 141) when the reader closes the pipe; a `gh` that cannot
  be executed and a local port that cannot be opened during `login` are reported as such
  (exit 2) and no longer as "filesystem error".
- Both actions set `PYTHONUTF8=1`: on a Windows runner an artifact name outside the
  console's code page no longer crashes the CLI.
- A boolean input that is neither true nor false is reported with its value escaped as
  workflow-command data.

## [0.1.0] - 2026-11-17

### Added

- `drive-artifacts` CLI: `login`, `upload`, `download`, `list`, `prune`, `logout`, `whoami`.
- Composite actions `TheChaosBureau/drive-artifacts` (upload) and `TheChaosBureau/drive-artifacts/download`.
- One zip per artifact, flat per-repository folder in Drive, identity in `appProperties`.
- Resumable uploads, ranged downloads, retries with full-jitter backoff, digest verification.
- Retention days with pruning, overwrite, `if-no-files-found`, hidden-file handling.
- `drive.file`-only OAuth with a shipped desktop client; token stored as a GitHub secret.

[Unreleased]: https://github.com/TheChaosBureau/drive-artifacts/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/TheChaosBureau/drive-artifacts/releases/tag/v0.1.0
