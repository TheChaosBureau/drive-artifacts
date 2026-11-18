# Security

Report vulnerabilities privately through GitHub's "Report a vulnerability" button
on this repository (Security → Advisories). Expect an acknowledgement within a
week.

What the package handles: a Google OAuth refresh token (read from the
`DRIVE_ARTIFACTS_TOKEN` environment variable only, never argv or files), the
derived access tokens (memory only, masked in Actions logs) and the bytes of your
artifacts. The app requests the `drive.file` scope only. Downloads are verified
against a recorded sha256 before extraction, and extraction rejects absolute
paths, `..`, and archives over fixed ceilings (200,000 members, 64 GiB expanded).

Supported: the latest minor release.
