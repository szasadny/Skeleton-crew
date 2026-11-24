from pathlib import Path
from typing import Any

from skeleton_crew.core.config import BootstrapConfig

FAKE_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$fake"  # noqa: S105


def make_config(data_dir: Path, **overrides: Any) -> BootstrapConfig:
    values: dict[str, Any] = {
        "repo": "acme/widgets",
        "github_app_id": 1,
        "github_app_private_key_path": "/nonexistent/key.pem",
        "ui_password_hash": FAKE_PASSWORD_HASH,
        "session_secret": "s" * 32,
        "data_dir": data_dir,
    }
    return BootstrapConfig.model_validate(values | overrides)
