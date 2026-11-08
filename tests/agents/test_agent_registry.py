from dataclasses import replace

import pytest
from pydantic import SecretStr

from skeleton_crew.agents import registry
from skeleton_crew.agents.base import BackendSpec, OptionSpec
from skeleton_crew.agents.registry import (
    UnknownBackendError,
    allowed_models,
    available_backends,
    backend_for,
    field_problems,
    settings_problems,
    spec_for,
)
from skeleton_crew.domain.settings import AppSettings, Role, RoleAgent

_CREDENTIALS = {
    "claude_oauth_token": SecretStr("oauth-token-placeholder"),
    "anthropic_api_key": SecretStr("api-key-placeholder"),
    "other_backend_key": SecretStr("other-key-placeholder"),
}


def _with_worker(**fields: object) -> AppSettings:
    settings = AppSettings()
    worker = settings.agents[Role.WORKER].model_copy(update=fields)
    return settings.model_copy(update={"agents": {**settings.agents, Role.WORKER: worker}})


def _with_options(options: dict[str, dict[str, str]]) -> AppSettings:
    return AppSettings(backend_options=options)


def test_domain_defaults_are_valid_for_the_registry() -> None:
    assert settings_problems(AppSettings()) == []


def test_every_spec_is_consistent() -> None:
    for spec in available_backends():
        assert spec_for(spec.name) is spec
        assert spec.models
        assert spec.efforts
        for option in spec.options.values():
            assert option.default in option.allowed
        RoleAgent(backend=spec.name, model=spec.models[0], effort=spec.efforts[0])


def test_every_backend_resolves_with_default_options() -> None:
    for spec in available_backends():
        backend = backend_for(spec.name, credentials=_CREDENTIALS, options={})
        assert backend.name == spec.name


def test_auth_mode_option_selects_the_credential() -> None:
    api = backend_for("claude_code", credentials=_CREDENTIALS, options={"auth_mode": "api_key"})
    sub = backend_for(
        "claude_code",
        credentials={"anthropic_api_key": _CREDENTIALS["anthropic_api_key"]},
        options={},
    )
    assert api.credential_problems() == []
    assert api.env()["ANTHROPIC_API_KEY"] == "api-key-placeholder"
    assert sub.credential_problems() != []


def test_backend_never_sees_undeclared_credentials() -> None:
    backend = backend_for("claude_code", credentials=_CREDENTIALS, options={})
    assert "other-key-placeholder" not in backend.env().values()


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(UnknownBackendError):
        backend_for("nope", credentials=_CREDENTIALS, options={})
    problems = field_problems(_with_worker(backend="nope"))
    assert problems == {"agents.worker.backend": ["unknown agent backend nope"]}


def test_unknown_model_and_effort_are_problems() -> None:
    problems = field_problems(_with_worker(model="gpt-9", effort="ludicrous"))
    assert set(problems) == {"agents.worker.model", "agents.worker.effort"}


def test_model_of_one_backend_is_not_valid_case_insensitively() -> None:
    assert "agents.worker.model" in field_problems(_with_worker(model="OPUS"))


@pytest.mark.parametrize(
    "options",
    [
        {"claude_code": {"auth_mode": "root"}},
        {"claude_code": {"auth_modes": "api_key"}},
        {"claude_code": {"auth_mode": "api_key", "extra": "1"}},
        {"ghost_backend": {"auth_mode": "api_key"}},
    ],
)
def test_unknown_option_key_value_or_backend_is_a_problem(
    options: dict[str, dict[str, str]],
) -> None:
    assert settings_problems(_with_options(options))


@pytest.mark.parametrize("options", [{"auth_mode": "root"}, {"auth_mode": "api_key", "x": "1"}])
def test_backend_for_refuses_invalid_options(options: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="invalid options"):
        backend_for("claude_code", credentials=_CREDENTIALS, options=options)


def test_valid_options_have_no_problems() -> None:
    assert settings_problems(_with_options({"claude_code": {"auth_mode": "api_key"}})) == []


def test_settings_problems_name_the_field() -> None:
    [problem] = settings_problems(_with_worker(model="gpt-9"))
    assert problem.startswith("agents.worker.model: ")


def _filtered_spec() -> BackendSpec:
    base = spec_for("claude_code")
    return replace(
        base,
        name="filtered",
        display_name="Filtered",
        models=("a/one", "b/one"),
        options={"provider": OptionSpec(allowed=("a", "b"), default="a")},
        model_filter=lambda model, options: model.startswith(f"{options['provider']}/"),
    )


@pytest.fixture
def filtered(monkeypatch: pytest.MonkeyPatch) -> BackendSpec:
    spec = _filtered_spec()
    monkeypatch.setattr(registry, "_BACKENDS", {**registry._BACKENDS, spec.name: spec})
    return spec


def _filtered_settings(model: str, options: dict[str, str] | None = None) -> AppSettings:
    settings = _with_worker(backend="filtered", model=model, effort="low")
    return settings.model_copy(
        update={"backend_options": {} if options is None else {"filtered": options}}
    )


@pytest.mark.usefixtures("filtered")
def test_model_filter_uses_the_default_option() -> None:
    assert "agents.worker.model" not in field_problems(_filtered_settings("a/one"))
    problems = field_problems(_filtered_settings("b/one"))
    assert problems["agents.worker.model"] == ["not a Filtered model for provider=a"]


@pytest.mark.usefixtures("filtered")
def test_model_filter_follows_the_chosen_option() -> None:
    chosen = {"provider": "b"}
    assert "agents.worker.model" not in field_problems(_filtered_settings("b/one", chosen))
    assert "agents.worker.model" in field_problems(_filtered_settings("a/one", chosen))


@pytest.mark.usefixtures("filtered")
def test_model_filter_cannot_be_bypassed_with_an_unlisted_model() -> None:
    problems = field_problems(_filtered_settings("b/two", {"provider": "b"}))
    assert problems["agents.worker.model"] == ["not a Filtered model"]


@pytest.mark.usefixtures("filtered")
def test_invalid_option_is_reported_once_not_as_a_model_problem() -> None:
    problems = field_problems(_filtered_settings("a/one", {"provider": "c"}))
    assert "agents.worker.model" not in problems
    assert "backend_options.filtered.provider" in problems


def test_allowed_models_follow_the_option_and_fall_back_on_invalid_options() -> None:
    spec = _filtered_spec()
    assert allowed_models(spec, {}) == ("a/one",)
    assert allowed_models(spec, {"provider": "b"}) == ("b/one",)
    assert allowed_models(spec, {"provider": "c"}) == spec.models
    assert allowed_models(spec_for("claude_code"), {}) == spec_for("claude_code").models
