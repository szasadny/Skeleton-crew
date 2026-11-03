from skeleton_crew.agents.backends import claude_code
from skeleton_crew.agents.backends.claude_code import ClaudeCodeBackend
from skeleton_crew.agents.base import AgentBackend
from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.domain.settings import AgentBackendName, ClaudeAuthMode


def backend_for(
    name: AgentBackendName, *, config: BootstrapConfig, auth_mode: ClaudeAuthMode
) -> AgentBackend:
    match name:
        case AgentBackendName.CLAUDE_CODE:
            return ClaudeCodeBackend(
                auth_mode=auth_mode,
                oauth_token=config.claude_oauth_token,
                api_key=config.anthropic_api_key,
            )


def models_for(name: AgentBackendName) -> tuple[str, ...]:
    match name:
        case AgentBackendName.CLAUDE_CODE:
            return claude_code.MODELS
