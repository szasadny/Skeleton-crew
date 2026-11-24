from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from web_helpers import make_config

from skeleton_crew.core.config import (
    BootstrapConfig,
    credential_setting,
    get_config,
    secret_value,
)

_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g"
_SESSION_SECRET = "s" * 32
_DATA = Path("/data")


def test_valid_config_derives_paths_and_repo_url() -> None:
    config = make_config(_DATA)
    assert config.repo_url == "https://github.com/acme/widgets"
    assert config.tasks_dir == Path("/data/tasks")
    assert config.db_path.parent == Path("/data")
    assert config.egress_allowlist_path == Path("/data/egress/allowlist.txt")


def test_missing_private_key_file_is_not_checked_at_load() -> None:
    assert not make_config(_DATA).github_app_private_key_path.exists()


@pytest.mark.parametrize(
    "repo",
    ["acme", "acme/", "/widgets", "acme/widgets/x", "ac me/widgets", "acme/..", "-acme/w", ""],
)
def test_invalid_repo_is_rejected(repo: str) -> None:
    with pytest.raises(ValidationError):
        make_config(_DATA, repo=repo)


def test_short_session_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_config(_DATA, session_secret="s" * 31)


def test_plain_password_instead_of_hash_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_config(_DATA, ui_password_hash="hunter2hunter2")  # noqa: S106


def test_invalid_bot_login_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_config(_DATA, github_bot_login="bad login")


def test_secret_values_lists_set_secrets_and_repr_hides_them() -> None:
    config = make_config(_DATA, agent_credentials={"some_key": "sk-test-not-a-real-key"})
    assert "sk-test-not-a-real-key" in config.secret_values()
    assert _SESSION_SECRET in config.secret_values()
    assert "sk-test-not-a-real-key" not in repr(config)


@pytest.mark.parametrize("name", ["bad-name", "1key", "a b", ""])
def test_invalid_credential_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        make_config(_DATA, agent_credentials={name: "fake-credential-value"})


def test_credential_names_differing_only_in_case_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_config(_DATA, agent_credentials={"key": "fake-value-one", "KEY": "fake-value-two"})


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SC_REPO", "acme/widgets")
    monkeypatch.setenv("SC_GITHUB_APP_ID", "7")
    monkeypatch.setenv("SC_GITHUB_APP_PRIVATE_KEY_PATH", "/nonexistent/key.pem")
    monkeypatch.setenv("SC_UI_PASSWORD_HASH", _HASH)
    monkeypatch.setenv("SC_SESSION_SECRET", _SESSION_SECRET)


def test_agent_credentials_come_from_nested_env_and_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SC_AGENT_CREDENTIALS__FIRST_TOKEN", "fake-first-credential")
    (tmp_path / ".env").write_text("SC_AGENT_CREDENTIALS__Second_Key=fake-second-credential\n")
    config = BootstrapConfig()  # type: ignore[call-arg]
    assert {k: v.get_secret_value() for k, v in config.agent_credentials.items()} == {
        "first_token": "fake-first-credential",
        "second_key": "fake-second-credential",
    }
    assert {"fake-first-credential", "fake-second-credential"} <= set(config.secret_values())
    assert "fake-first-credential" not in repr(config)


def test_get_config_reads_prefixed_env_and_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    get_config.cache_clear()
    try:
        assert get_config().github_app_id == 7
        assert get_config() is get_config()
    finally:
        get_config.cache_clear()


def test_required_dirs_hold_every_data_directory() -> None:
    config = make_config(_DATA, runner_allowlist_file="/etc/runners/allowlist.txt")
    assert config.required_dirs == (
        Path("/data"),
        Path("/data/tasks"),
        Path("/data/egress"),
        Path("/data/runners"),
        Path("/etc/runners"),
    )


def test_credential_setting_names_the_env_variable_that_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert credential_setting("claude_oauth_token") == "SC_AGENT_CREDENTIALS__CLAUDE_OAUTH_TOKEN"
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv(credential_setting("some_key"), "fake-credential-value")
    config = BootstrapConfig()  # type: ignore[call-arg]
    assert config.agent_credentials["some_key"].get_secret_value() == "fake-credential-value"


def test_secret_value_strips_and_treats_blank_as_missing() -> None:
    credentials = {"a": SecretStr(" key \n"), "b": SecretStr("   ")}
    assert secret_value(credentials.get("a")) == "key"
    assert secret_value(credentials.get("b")) is None
    assert secret_value(credentials.get("c")) is None
