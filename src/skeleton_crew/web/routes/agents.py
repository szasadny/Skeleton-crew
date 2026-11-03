from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.agents.registry import models_for
from skeleton_crew.domain.settings import (
    AgentBackendName,
    AppSettings,
    ClaudeAuthMode,
    Effort,
    Role,
)
from skeleton_crew.web.forms import Kind, SettingsPage

router = APIRouter()

_ROLE_FIELDS = {
    "backend": Kind.TEXT,
    "model": Kind.TEXT,
    "effort": Kind.TEXT,
    "web_tools": Kind.BOOL,
}


def _unknown_models(settings: AppSettings) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for role, agent in settings.agents.items():
        if agent.model not in models_for(agent.backend):
            errors[f"agents.{role}.model"] = [f"not a {agent.backend} model"]
    return errors


_PAGE = SettingsPage(
    template="agents.html",
    fields={
        **{f"agents.{role}.{name}": kind for role in Role for name, kind in _ROLE_FIELDS.items()},
        "claude_auth_mode": Kind.TEXT,
    },
    context={
        "roles": tuple(Role),
        "backends": tuple(AgentBackendName),
        "models": {backend.value: models_for(backend) for backend in AgentBackendName},
        "efforts": tuple(Effort),
        "auth_modes": tuple(ClaudeAuthMode),
    },
    check=_unknown_models,
)


@router.get("/agents")
async def show(request: Request) -> Response:
    return await _PAGE.show(request)


@router.post("/agents")
async def submit(request: Request) -> Response:
    return await _PAGE.submit(request)
