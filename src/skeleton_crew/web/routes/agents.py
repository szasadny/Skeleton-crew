from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import SecretStr

from skeleton_crew.agents.base import BackendSpec
from skeleton_crew.agents.registry import (
    allowed_models,
    available_backends,
    backend_for,
    field_problems,
    option_problems,
)
from skeleton_crew.classifiers import registry as classifiers
from skeleton_crew.domain.settings import AppSettings, Role
from skeleton_crew.web.forms import GENERAL, Kind, SettingsPage
from skeleton_crew.web.state import get_state

router = APIRouter()

_ROLE_FIELDS = {
    "backend": Kind.TEXT,
    "model": Kind.TEXT,
    "effort": Kind.TEXT,
    "web_tools": Kind.BOOL,
}
_USE_CASES = ("injection_screen", "prescreen", "effort_routing")
_CLASSIFIER_FIELDS = {
    "provider": Kind.TEXT,
    "model": Kind.TEXT,
    **{name: Kind.BOOL for name in _USE_CASES},
    "effort_small": Kind.TEXT,
    "effort_medium": Kind.TEXT,
    "effort_large": Kind.TEXT,
}
_ROLE_DUTIES = {
    Role.SELECTOR: "Picks which labelled issues a run takes on, and in what order.",
    Role.REFINER: "Turns issues labelled skeleton refine into a spec for the owner.",
    Role.REVIEWER: "Reviews each pull request from a fresh, read-only copy.",
    Role.WORKER: "Writes the code and opens the pull request.",
}
_BACKENDS = {spec.name: spec for spec in available_backends()}
_CLASSIFIERS = {spec.name: spec for spec in classifiers.available_classifiers()}
_FIELDS = {
    **{f"agents.{role}.{name}": kind for role in Role for name, kind in _ROLE_FIELDS.items()},
    **{
        f"backend_options.{spec.name}.{option}": Kind.TEXT
        for spec in _BACKENDS.values()
        for option in spec.options
    },
    **{f"classifier.{name}": kind for name, kind in _CLASSIFIER_FIELDS.items()},
}


@dataclass(frozen=True)
class RoleView:
    role: Role
    duty: str
    spec: BackendSpec | None
    models: tuple[str, ...]


@dataclass(frozen=True)
class BackendView:
    spec: BackendSpec
    used_by: tuple[Role, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True)
class ClassifierView:
    models: tuple[str, ...]
    enabled: bool
    problems: tuple[str, ...]


def _options(spec: BackendSpec, values: Mapping[str, object]) -> dict[str, str]:
    return {
        name: str(values.get(f"backend_options.{spec.name}.{name}") or option.default)
        for name, option in spec.options.items()
    }


def _credential_problems(
    spec: BackendSpec, options: Mapping[str, str], credentials: Mapping[str, SecretStr]
) -> tuple[str, ...]:
    if option_problems(spec, options):
        return ()
    return tuple(
        backend_for(spec.name, credentials=credentials, options=options).credential_problems()
    )


def _roles(values: Mapping[str, object]) -> tuple[RoleView, ...]:
    views = []
    for role in Role:
        spec = _BACKENDS.get(str(values.get(f"agents.{role}.backend")))
        models = allowed_models(spec, _options(spec, values)) if spec else ()
        views.append(RoleView(role, _ROLE_DUTIES[role], spec, models))
    return tuple(views)


def _backends(
    roles: tuple[RoleView, ...], values: Mapping[str, object], credentials: Mapping[str, SecretStr]
) -> tuple[BackendView, ...]:
    views = []
    for spec in _BACKENDS.values():
        used_by = tuple(view.role for view in roles if view.spec is spec)
        problems = (
            _credential_problems(spec, _options(spec, values), credentials) if used_by else ()
        )
        views.append(BackendView(spec, used_by, problems))
    return tuple(views)


def _classifier(
    values: Mapping[str, object], credentials: Mapping[str, SecretStr]
) -> ClassifierView:
    provider = str(values.get("classifier.provider"))
    spec = _CLASSIFIERS.get(provider)
    enabled = any(values.get(f"classifier.{name}") for name in _USE_CASES)
    problems = classifiers.credential_problems(provider, credentials) if spec and enabled else []
    return ClassifierView(spec.models if spec else (), enabled, tuple(problems))


def _live(request: Request, values: Mapping[str, object]) -> Mapping[str, Any]:
    credentials = get_state(request).config.agent_credentials
    roles = _roles(values)
    worker = next(view.spec for view in roles if view.role is Role.WORKER)
    return {
        "roles": roles,
        "routed_efforts": worker.efforts if worker else (),
        "backends": _backends(roles, values, credentials),
        "classifier": _classifier(values, credentials),
    }


def _problems(settings: AppSettings) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    found = field_problems(settings) | classifiers.field_problems(settings)
    for name, messages in found.items():
        errors.setdefault(name if name in _FIELDS else GENERAL, []).extend(messages)
    return errors


_PAGE = SettingsPage(
    template="agents.html",
    fields=_FIELDS,
    context={"backend_names": tuple(_BACKENDS), "classifier_names": tuple(_CLASSIFIERS)},
    check=_problems,
    live_context=_live,
)


@router.get("/agents")
async def show(request: Request) -> Response:
    return await _PAGE.show(request)


@router.get("/agents/draft")
async def draft(request: Request) -> Response:
    return await _PAGE.draft(request)


@router.post("/agents")
async def submit(request: Request) -> Response:
    return await _PAGE.submit(request)
