from skeleton_crew.agents.backends.claude_code import ClaudeCodeBackend
from skeleton_crew.agents.registry import backend_for, models_for
from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.domain.settings import AgentBackendName, ClaudeAuthMode, RoleAgent


def _config() -> BootstrapConfig:
    return BootstrapConfig.model_validate(
        {
            "repo": "acme/widgets",
            "github_app_id": 1,
            "github_app_private_key_path": "/nonexistent/key.pem",
            "anthropic_api_key": "api-key-placeholder",
            "ui_password_hash": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA",
            "session_secret": "s" * 32,
        }
    )


def test_every_backend_name_resolves() -> None:
    for name in AgentBackendName:
        backend = backend_for(name, config=_config(), auth_mode=ClaudeAuthMode.API_KEY)
        assert backend.name is name


def test_every_backend_offers_the_default_model() -> None:
    for name in AgentBackendName:
        assert RoleAgent().model in models_for(name)


def test_claude_code_uses_config_credentials_for_the_auth_mode() -> None:
    config = _config()
    api = backend_for(AgentBackendName.CLAUDE_CODE, config=config, auth_mode=ClaudeAuthMode.API_KEY)
    sub = backend_for(
        AgentBackendName.CLAUDE_CODE, config=config, auth_mode=ClaudeAuthMode.SUBSCRIPTION
    )
    assert isinstance(api, ClaudeCodeBackend)
    assert api.credential_problems() == []
    assert sub.credential_problems() != []
