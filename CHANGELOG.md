# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- A link to a directory is no longer followed when a wildcard reaches it (`dist/*`, `**`):
  wildcards now see what a walk of the directory sees, on every supported Python, and a link
  back to an ancestor can no longer put many copies of a tree into one artifact. A link that
  `path` names itself is still walked.
- A pattern that climbs out of the directory it searches (`a/*/../../b`), and patterns on
  two Windows drives, are reported as usage errors (exit 2) instead of crashing.

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
