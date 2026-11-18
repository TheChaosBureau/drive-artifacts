# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Upload: a chunk the server stored although its answer was lost counts as progress even on
  the last attempt of the retry budget; a stall draws on one budget however many offset
  queries it takes; an upload session answered with 410 is reopened like one answered 404.
- Retries never sleep past the deadline, and downloads honour the deadline too.
- A download whose connection breaks after the last byte no longer waits once before
  finishing.
- A successful answer that is not the expected JSON (a proxy's or captive portal's page), a
  redirect, a malformed `Range` on an upload chunk and a malformed upload session URL are
  reported as Drive errors (exit 5). A redirect's body is never written as file content.
- A chunk size of zero or less is refused.

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
