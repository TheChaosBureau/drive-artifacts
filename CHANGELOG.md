# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `login`: a browser connection that opens and sends nothing (a preconnect) no longer keeps
  the real redirect waiting until the login times out; each connection may stay silent for
  at most 5 seconds.
- `login`: the page shown in the browser now says when the login failed (Google reported an
  error, or the response carried no code) instead of always "you can close this tab", and a
  redirect that is not a GET is named in the timeout message.
- Google's token endpoint answering 429 is retried like a 5xx.
- In Actions, a masked value is encoded like any other command data, so a value containing
  `%` or a line break is masked as itself; an output value containing a lone carriage
  return is written in the multi-line form.
- The "not inside GitHub Actions" message names only the option that is missing.

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
