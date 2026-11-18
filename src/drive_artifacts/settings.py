# SPDX-License-Identifier: Apache-2.0
"""Everything the package reads from the environment, resolved once."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from drive_artifacts import _client

GOOGLE_API = "https://www.googleapis.com"
GOOGLE_OAUTH = "https://oauth2.googleapis.com"
GOOGLE_AUTH_PAGE = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive.file"
DEFAULT_ROOT_NAME = "GitHub Artifacts"  # the folder the app owns in the user's Drive


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration. Construct with `from_env`."""

    api_base: str
    upload_base: str
    token_url: str
    revoke_url: str
    auth_url: str
    client_id: str
    client_secret: str = field(repr=False)
    token_text: str | None = field(repr=False)
    root_name: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        """Read every `DRIVE_ARTIFACTS_*` variable; an empty one counts as unset."""
        api = (env.get("DRIVE_ARTIFACTS_API_BASE") or GOOGLE_API).rstrip("/")
        oauth = env.get("DRIVE_ARTIFACTS_OAUTH_BASE")
        if not oauth:
            token_url, revoke_url, auth_url = (
                f"{GOOGLE_OAUTH}/token",
                f"{GOOGLE_OAUTH}/revoke",
                GOOGLE_AUTH_PAGE,
            )
        else:
            oauth = oauth.rstrip("/")
            token_url, revoke_url, auth_url = (
                f"{oauth}/token",
                f"{oauth}/revoke",
                f"{oauth}/auth",
            )
        token = env.get("DRIVE_ARTIFACTS_TOKEN", "").strip() or None
        return cls(
            api_base=f"{api}/drive/v3",
            upload_base=f"{api}/upload/drive/v3",
            token_url=token_url,
            revoke_url=revoke_url,
            auth_url=auth_url,
            client_id=env.get("DRIVE_ARTIFACTS_CLIENT_ID") or _client.CLIENT_ID,
            client_secret=env.get("DRIVE_ARTIFACTS_CLIENT_SECRET") or _client.CLIENT_SECRET,
            token_text=token,
            root_name=env.get("DRIVE_ARTIFACTS_ROOT") or DEFAULT_ROOT_NAME,
        )
