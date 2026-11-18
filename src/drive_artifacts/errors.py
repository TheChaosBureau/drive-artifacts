# SPDX-License-Identifier: Apache-2.0
"""The exception hierarchy. Every user-visible failure is one of these, mapped to one exit code."""

from __future__ import annotations


class DriveArtifactsError(Exception):
    """Base class. `exit_code` is what the CLI returns for it."""

    exit_code = 1


class UsageError(DriveArtifactsError):
    """Bad arguments, bad environment, bad artifact name."""

    exit_code = 2


class AuthError(DriveArtifactsError):
    """The token is missing, malformed, revoked or minted by another client."""

    exit_code = 3


class NotFoundError(DriveArtifactsError):
    """No artifact matches the request."""

    exit_code = 4


class DriveError(DriveArtifactsError):
    """Drive answered with an error we could not retry away."""

    exit_code = 5

    def __init__(self, message: str, *, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class ConflictError(DriveArtifactsError):
    """An artifact of that name already exists for the run and overwrite was not asked for.

    The store refuses it; Drive is never asked. It shares exit code 5 with `DriveError`."""

    exit_code = 5


class IntegrityError(DriveArtifactsError):
    """Downloaded bytes do not match the recorded digest, or the zip is unsafe to extract."""

    exit_code = 6


class NoFilesError(DriveArtifactsError):
    """`path` matched nothing and `if-no-files-found` is `error`."""

    exit_code = 7
