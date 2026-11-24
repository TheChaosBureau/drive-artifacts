# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-24

### Added

- A "latest" mode for the upload action (`latest: true`) and the CLI (`upload --latest`):
  one plain file, not zipped and with no expiry, at a fixed path under the root folder
  (default `Latest/<owner>/<repo>/<file name>`, or `latest-path`). A later upload to the
  same path replaces the content of the same Drive file, so its id and link stay the same.
  The file records repository, commit, ref, run and upload time as app properties. The mode
  runs only on the default branch unless `latest-any-ref` (`--any-ref`) is set.
- `drive-artifacts latest` and `ArtifactStore.list_latest()` list every latest file the app
  can see, with its link and those properties; `--json` for machines.

## [0.1.0] - 2026-11-18

### Added

- `drive-artifacts` CLI: `login`, `upload`, `download`, `list`, `prune`, `logout`, `whoami`.
- Composite actions `TheChaosBureau/drive-artifacts` (upload) and `TheChaosBureau/drive-artifacts/download`.
- One zip per artifact, flat per-repository folder in Drive, identity in `appProperties`.
- Resumable uploads, ranged downloads, retries with full-jitter backoff, digest verification.
- Retention days with pruning, overwrite, `if-no-files-found`, hidden-file handling.
- `drive.file`-only OAuth with a shipped desktop client; token stored as a GitHub secret.

[Unreleased]: https://github.com/TheChaosBureau/drive-artifacts/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/TheChaosBureau/drive-artifacts/releases/tag/v0.2.0
[0.1.0]: https://github.com/TheChaosBureau/drive-artifacts/releases/tag/v0.1.0
