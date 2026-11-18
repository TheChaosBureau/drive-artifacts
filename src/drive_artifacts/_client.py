# SPDX-License-Identifier: Apache-2.0
"""The published OAuth client this package ships with.

The Chaos Bureau's Desktop-app client for the "Drive Artifacts" consent screen, which is
in production and asks for `drive.file` only. Google documents installed-app client
secrets as not secret (https://developers.google.com/identity/protocols/oauth2#installed),
so both values are committed on purpose. DRIVE_ARTIFACTS_CLIENT_ID/SECRET in the
environment override them, for a fork or a private client of your own.
"""

CLIENT_ID = "301726695005-8mrsj8dv6t4hpjkuadj28788i6itmrac.apps.googleusercontent.com"
# Not a credential: Google treats installed-app client secrets as public (see docstring).
CLIENT_SECRET = "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"  # noqa: S105
