import re
from functools import cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(\[bot\])?$")
_CREDENTIAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MIN_SESSION_SECRET_CHARS = 32
_MIN_REDACT_CHARS = 8


class BootstrapConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SC_", env_file=".env", env_nested_delimiter="__", extra="ignore"
    )

    repo: str
    github_app_id: int
    github_app_private_key_path: Path
    github_app_installation_id: int | None = None
    github_bot_login: str | None = None
    agent_credentials: dict[str, SecretStr] = {}
    ui_password_hash: SecretStr
    session_secret: SecretStr
    data_dir: Path = Path("/var/lib/skeleton-crew")
    agent_image: str = "skeleton-crew-agent:latest"
    agent_network: str = "skeleton-crew-agents"
    egress_proxy_url: str = "http://egress-proxy:3128"
    egress_proxy_container: str = "skeleton-crew-egress-proxy"
    egress_allowlist_file: Path | None = None
    host: str = "0.0.0.0"  # noqa: S104 -- the controller runs in a container behind Caddy
    port: int = 8080
    insecure_cookies: bool = False

    @field_validator("repo")
    @classmethod
    def _valid_repo(cls, value: str) -> str:
        name = value.partition("/")[2]
        if not _REPO_RE.fullmatch(value) or name in {".", ".."}:
            raise ValueError("repo must be 'owner/name'")
        return value

    @field_validator("github_bot_login")
    @classmethod
    def _valid_login(cls, value: str | None) -> str | None:
        if value is not None and not _LOGIN_RE.fullmatch(value):
            raise ValueError("invalid GitHub login")
        return value

    @field_validator("agent_credentials")
    @classmethod
    def _valid_credential_names(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        names = {key.lower(): secret for key, secret in value.items()}
        if len(names) != len(value) or not all(map(_CREDENTIAL_NAME_RE.fullmatch, names)):
            raise ValueError("agent credential names must be distinct lowercase identifiers")
        return names

    @field_validator("ui_password_hash")
    @classmethod
    def _argon2_hash(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("$argon2"):
            raise ValueError("ui_password_hash must be an argon2 hash (use `hash-password`)")
        return value

    @field_validator("session_secret")
    @classmethod
    def _long_session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < _MIN_SESSION_SECRET_CHARS:
            raise ValueError(f"session_secret must be at least {_MIN_SESSION_SECRET_CHARS} chars")
        return value

    @property
    def owner(self) -> str:
        return self.repo.partition("/")[0]

    @property
    def name(self) -> str:
        return self.repo.partition("/")[2]

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "skeleton-crew.db"

    @property
    def egress_allowlist_path(self) -> Path:
        return self.egress_allowlist_file or self.data_dir / "egress" / "allowlist.txt"

    def secret_values(self) -> list[str]:
        secrets = [*self.agent_credentials.values(), self.ui_password_hash, self.session_secret]
        return [value for s in secrets if len(value := s.get_secret_value()) >= _MIN_REDACT_CHARS]


@cache
def get_config() -> BootstrapConfig:
    return BootstrapConfig()  # type: ignore[call-arg]  # required fields come from env/.env
