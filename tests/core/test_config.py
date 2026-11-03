from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from skeleton_crew.core.config import BootstrapConfig, get_config

_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g"
_SESSION_SECRET = "s" * 32


def _config(**overrides: Any) -> BootstrapConfig:
    values: dict[str, Any] = {
        "repo": "acme/widgets",
        "github_app_id": 1,
        "github_app_private_key_path": "/nonexistent/key.pem",
        "ui_password_hash": _HASH,
        "session_secret": _SESSION_SECRET,
    }
    values.update(overrides)
    return BootstrapConfig.model_validate(values)


def test_valid_config_derives_paths_and_repo_parts() -> None:
    config = _config(data_dir="/data")
    assert (config.owner, config.name) == ("acme", "widgets")
    assert config.tasks_dir == Path("/data/tasks")
    assert config.db_path.parent == Path("/data")
    assert config.egress_allowlist_path == Path("/data/egress/allowlist.txt")


def test_missing_private_key_file_is_not_checked_at_load() -> None:
    assert not _config().github_app_private_key_path.exists()


@pytest.mark.parametrize(
    "repo",
    ["acme", "acme/", "/widgets", "acme/widgets/x", "ac me/widgets", "acme/..", "-acme/w", ""],
)
def test_invalid_repo_is_rejected(repo: str) -> None:
    with pytest.raises(ValidationError):
        _config(repo=repo)


def test_short_session_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(session_secret="s" * 31)


def test_plain_password_instead_of_hash_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(ui_password_hash="hunter2hunter2")  # noqa: S106


def test_invalid_bot_login_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(github_bot_login="bad login")


def test_secret_values_lists_set_secrets_and_repr_hides_them() -> None:
    config = _config(anthropic_api_key="sk-test-not-a-real-key")
    assert "sk-test-not-a-real-key" in config.secret_values()
    assert _SESSION_SECRET in config.secret_values()
    assert "sk-test-not-a-real-key" not in repr(config)


def test_get_config_reads_prefixed_env_and_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SC_REPO", "acme/widgets")
    monkeypatch.setenv("SC_GITHUB_APP_ID", "7")
    monkeypatch.setenv("SC_GITHUB_APP_PRIVATE_KEY_PATH", "/nonexistent/key.pem")
    monkeypatch.setenv("SC_UI_PASSWORD_HASH", _HASH)
    monkeypatch.setenv("SC_SESSION_SECRET", _SESSION_SECRET)
    get_config.cache_clear()
    try:
        assert get_config().github_app_id == 7
        assert get_config() is get_config()
    finally:
        get_config.cache_clear()
