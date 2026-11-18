# SPDX-License-Identifier: Apache-2.0
from drive_artifacts import _client
from drive_artifacts.settings import DEFAULT_ROOT_NAME, Settings


def test_defaults_point_at_google() -> None:
    s = Settings.from_env({})
    assert s.api_base == "https://www.googleapis.com/drive/v3"
    assert s.upload_base == "https://www.googleapis.com/upload/drive/v3"
    assert s.token_url == "https://oauth2.googleapis.com/token"
    assert s.revoke_url == "https://oauth2.googleapis.com/revoke"
    assert s.auth_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert s.client_id == _client.CLIENT_ID
    assert s.client_secret == _client.CLIENT_SECRET
    assert s.token_text is None
    assert s.root_name == DEFAULT_ROOT_NAME == "GitHub Artifacts"


def test_env_overrides_every_endpoint_and_the_client() -> None:
    s = Settings.from_env(
        {
            "DRIVE_ARTIFACTS_API_BASE": "http://127.0.0.1:9/api",
            "DRIVE_ARTIFACTS_OAUTH_BASE": "http://127.0.0.1:9/oauth",
            "DRIVE_ARTIFACTS_CLIENT_ID": "cid",
            "DRIVE_ARTIFACTS_CLIENT_SECRET": "csec",
            "DRIVE_ARTIFACTS_TOKEN": '{"v": 1}',
            "DRIVE_ARTIFACTS_ROOT": "My CI",
        }
    )
    assert s.api_base == "http://127.0.0.1:9/api/drive/v3"
    assert s.upload_base == "http://127.0.0.1:9/api/upload/drive/v3"
    assert s.token_url == "http://127.0.0.1:9/oauth/token"
    assert s.revoke_url == "http://127.0.0.1:9/oauth/revoke"
    assert s.auth_url == "http://127.0.0.1:9/oauth/auth"
    assert (s.client_id, s.client_secret) == ("cid", "csec")
    assert s.token_text == '{"v": 1}'
    assert s.root_name == "My CI"


def test_trailing_slashes_are_tolerated() -> None:
    s = Settings.from_env({"DRIVE_ARTIFACTS_API_BASE": "http://h/api/"})
    assert s.api_base == "http://h/api/drive/v3"


def test_empty_token_is_none() -> None:
    assert Settings.from_env({"DRIVE_ARTIFACTS_TOKEN": "  "}).token_text is None


def test_empty_variables_mean_unset() -> None:
    blank = Settings.from_env(
        {
            "DRIVE_ARTIFACTS_API_BASE": "",
            "DRIVE_ARTIFACTS_OAUTH_BASE": "",
            "DRIVE_ARTIFACTS_CLIENT_ID": "",
            "DRIVE_ARTIFACTS_CLIENT_SECRET": "",
            "DRIVE_ARTIFACTS_TOKEN": "",
            "DRIVE_ARTIFACTS_ROOT": "",
        }
    )
    assert blank.api_base == "https://www.googleapis.com/drive/v3"
    assert blank.token_url == "https://oauth2.googleapis.com/token"
    assert blank == Settings.from_env({})


def test_repr_hides_the_token_and_client_secret() -> None:
    s = Settings.from_env(
        {
            "DRIVE_ARTIFACTS_CLIENT_SECRET": "csec-LEAKCHECK",
            "DRIVE_ARTIFACTS_TOKEN": '{"refresh_token": "tok-LEAKCHECK"}',
        }
    )
    text = repr(s)
    assert "csec-LEAKCHECK" not in text
    assert "tok-LEAKCHECK" not in text
    assert "api_base='https://www.googleapis.com/drive/v3'" in text


def test_one_base_can_be_overridden_without_the_other() -> None:
    api_only = Settings.from_env({"DRIVE_ARTIFACTS_API_BASE": "http://h/api"})
    assert api_only.api_base == "http://h/api/drive/v3"
    assert api_only.token_url == "https://oauth2.googleapis.com/token"
    assert api_only.auth_url == "https://accounts.google.com/o/oauth2/v2/auth"
    oauth_only = Settings.from_env({"DRIVE_ARTIFACTS_OAUTH_BASE": "http://h/oauth"})
    assert oauth_only.api_base == "https://www.googleapis.com/drive/v3"
    assert oauth_only.upload_base == "https://www.googleapis.com/upload/drive/v3"
    assert oauth_only.token_url == "http://h/oauth/token"


def test_a_trailing_slash_on_the_oauth_base_is_tolerated() -> None:
    s = Settings.from_env({"DRIVE_ARTIFACTS_OAUTH_BASE": "http://h/oauth//"})
    assert (s.token_url, s.revoke_url, s.auth_url) == (
        "http://h/oauth/token",
        "http://h/oauth/revoke",
        "http://h/oauth/auth",
    )


def test_whitespace_around_the_token_is_stripped() -> None:
    s = Settings.from_env({"DRIVE_ARTIFACTS_TOKEN": '  {"v": 1}\n'})
    assert s.token_text == '{"v": 1}'


def test_an_empty_client_id_or_secret_falls_back_to_the_shipped_client() -> None:
    s = Settings.from_env({"DRIVE_ARTIFACTS_CLIENT_ID": "", "DRIVE_ARTIFACTS_CLIENT_SECRET": "x"})
    assert (s.client_id, s.client_secret) == (_client.CLIENT_ID, "x")
    s = Settings.from_env({"DRIVE_ARTIFACTS_CLIENT_ID": "x", "DRIVE_ARTIFACTS_CLIENT_SECRET": ""})
    assert (s.client_id, s.client_secret) == ("x", _client.CLIENT_SECRET)
