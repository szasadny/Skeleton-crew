from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from skeleton_crew.agents.registry import (
    BackendSpec,
    allowed_models,
    available_backends,
    credential_problems,
    resolved_options,
)
from skeleton_crew.classifiers import registry as classifiers
from skeleton_crew.domain.settings import CLASSIFIER_USE_CASES, Role

BACKENDS = {spec.name: spec for spec in available_backends()}
CLASSIFIERS = {spec.name: spec for spec in classifiers.available_classifiers()}
_ROLE_DUTIES = {
    Role.SELECTOR: "Picks which labelled issues a run takes on, and in what order.",
    Role.REFINER: "Turns issues labelled skeleton refine into a spec for the owner.",
    Role.REVIEWER: "Reviews each pull request from a fresh, read-only copy.",
    Role.WORKER: "Writes the code and opens the pull request.",
}
_READY = "ready"
_NO_CREDENTIAL = "no credential"


@dataclass(frozen=True)
class RoleView:
    role: Role
    duty: str
    spec: BackendSpec | None
    models: tuple[str, ...]


@dataclass(frozen=True)
class BackendView:
    spec: BackendSpec
    options: dict[str, str]
    used_by: tuple[Role, ...]
    problems: tuple[str, ...]

    @property
    def status(self) -> str:
        if not self.used_by:
            return "unused"
        return _NO_CREDENTIAL if self.problems else _READY


@dataclass(frozen=True)
class ClassifierView:
    models: tuple[str, ...]
    enabled: bool
    problems: tuple[str, ...]

    @property
    def status(self) -> str:
        if not self.enabled:
            return "off"
        return _NO_CREDENTIAL if self.problems else _READY


def _options(spec: BackendSpec, values: Mapping[str, object]) -> dict[str, str]:
    chosen = {
        name: str(value)
        for name in spec.options
        if (value := values.get(f"backend_options.{spec.name}.{name}"))
    }
    return resolved_options(spec, chosen)


def _roles(values: Mapping[str, object]) -> tuple[RoleView, ...]:
    views = []
    for role in Role:
        spec = BACKENDS.get(str(values.get(f"agents.{role}.backend")))
        models = allowed_models(spec, _options(spec, values)) if spec else ()
        views.append(RoleView(role, _ROLE_DUTIES[role], spec, models))
    return tuple(views)


def _backends(
    roles: tuple[RoleView, ...], values: Mapping[str, object], credentials: Mapping[str, SecretStr]
) -> tuple[BackendView, ...]:
    views = []
    for spec in BACKENDS.values():
        options = _options(spec, values)
        used_by = tuple(view.role for view in roles if view.spec is spec)
        problems = credential_problems(spec, options, credentials) if used_by else ()
        views.append(BackendView(spec, options, used_by, problems))
    return tuple(views)


def _classifier(
    values: Mapping[str, object], credentials: Mapping[str, SecretStr]
) -> ClassifierView:
    provider = str(values.get("classifier.provider"))
    spec = CLASSIFIERS.get(provider)
    enabled = any(values.get(f"classifier.{name}") for name in CLASSIFIER_USE_CASES)
    problems = classifiers.credential_problems(provider, credentials) if spec and enabled else []
    return ClassifierView(spec.models if spec else (), enabled, tuple(problems))


def agents_context(
    values: Mapping[str, object], credentials: Mapping[str, SecretStr]
) -> dict[str, Any]:
    roles = _roles(values)
    worker = next(view.spec for view in roles if view.role is Role.WORKER)
    return {
        "roles": roles,
        "routed_efforts": worker.efforts if worker else (),
        "backends": _backends(roles, values, credentials),
        "classifier": _classifier(values, credentials),
    }
