from collections.abc import Mapping

from pydantic import SecretStr

from skeleton_crew.agents.backends import claude_code
from skeleton_crew.agents.base import AgentBackend, BackendSpec
from skeleton_crew.domain.settings import AppSettings

_BACKENDS: Mapping[str, BackendSpec] = {spec.name: spec for spec in (claude_code.SPEC,)}


class UnknownBackendError(ValueError):
    pass


def available_backends() -> tuple[BackendSpec, ...]:
    return tuple(_BACKENDS.values())


def spec_for(name: str) -> BackendSpec:
    try:
        return _BACKENDS[name]
    except KeyError:
        raise UnknownBackendError(f"unknown agent backend: {name}") from None


def option_problems(spec: BackendSpec, options: Mapping[str, str]) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for key, value in options.items():
        field = f"backend_options.{spec.name}.{key}"
        option = spec.options.get(key)
        if option is None:
            errors.setdefault(field, []).append(f"unknown {spec.display_name} option")
        elif value not in option.allowed:
            errors.setdefault(field, []).append(f"not an allowed {spec.display_name} value")
    return errors


def field_problems(settings: AppSettings) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for role, agent in settings.agents.items():
        prefix = f"agents.{role}"
        spec = _BACKENDS.get(agent.backend)
        if spec is None:
            errors[f"{prefix}.backend"] = [f"unknown agent backend {agent.backend}"]
            continue
        if agent.model not in spec.models:
            errors[f"{prefix}.model"] = [f"not a {spec.display_name} model"]
        if agent.effort not in spec.efforts:
            errors[f"{prefix}.effort"] = [f"not a {spec.display_name} effort"]
        if agent.web_tools and not spec.web_tools:
            errors[f"{prefix}.web_tools"] = [f"{spec.display_name} has no web tools"]
    for name, options in settings.backend_options.items():
        spec = _BACKENDS.get(name)
        if spec is None:
            errors[f"backend_options.{name}"] = [f"unknown agent backend {name}"]
        else:
            errors |= option_problems(spec, options)
    return errors


def settings_problems(settings: AppSettings) -> list[str]:
    return [
        f"{field}: {message}"
        for field, messages in field_problems(settings).items()
        for message in messages
    ]


def backend_for(
    name: str, *, credentials: Mapping[str, SecretStr], options: Mapping[str, str]
) -> AgentBackend:
    spec = spec_for(name)
    problems = option_problems(spec, options)
    if problems:
        raise ValueError(f"invalid options for {name}: {sorted(problems)}")
    resolved = {key: option.default for key, option in spec.options.items()} | dict(options)
    # A backend only ever sees the credentials it declares.
    own = {key: credentials[key] for key in spec.credentials if key in credentials}
    return spec.factory(own, resolved)
