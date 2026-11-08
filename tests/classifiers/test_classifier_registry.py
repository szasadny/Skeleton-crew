import pytest
from pydantic import SecretStr, ValidationError

from skeleton_crew.agents.registry import field_problems as agent_field_problems
from skeleton_crew.classifiers.registry import (
    available_classifiers,
    credential_problems,
    field_problems,
)
from skeleton_crew.domain.settings import (
    DEFAULT_CLASSIFIER,
    DEFAULT_CLASSIFIER_MODEL,
    AppSettings,
    ClassifierSettings,
    Role,
    RoleAgent,
)


def _settings(**values: object) -> AppSettings:
    return AppSettings(classifier=ClassifierSettings.model_validate(values))


def test_defaults_are_a_registered_provider_and_model() -> None:
    specs = {spec.name: spec for spec in available_classifiers()}
    assert DEFAULT_CLASSIFIER_MODEL in specs[DEFAULT_CLASSIFIER].models
    assert field_problems(AppSettings()) == {}


def test_every_use_case_is_off_by_default() -> None:
    chosen = ClassifierSettings()
    assert (chosen.injection_screen, chosen.prescreen, chosen.effort_routing) == (False,) * 3
    assert chosen.enabled is False


def test_unknown_provider_and_foreign_model_are_rejected() -> None:
    assert field_problems(_settings(provider="nope")) == {
        "classifier.provider": ["unknown classifier nope"]
    }
    assert field_problems(_settings(provider="openai_decisions", model="jev-latest")) == {
        "classifier.model": ["not a OpenAI Decisions model"]
    }


@pytest.mark.parametrize("effort", ["", "x" * 40, "low; rm -rf /"])
def test_routed_efforts_must_look_like_an_effort(effort: str) -> None:
    with pytest.raises(ValidationError):
        ClassifierSettings(effort_small=effort)


def test_routed_efforts_must_suit_the_worker_backend_when_routing_is_on() -> None:
    gemini = {**AppSettings().agents, Role.WORKER: RoleAgent(backend="gemini", effort="default")}
    gemini_worker = AppSettings(agents=gemini, classifier=ClassifierSettings(effort_routing=True))
    problems = agent_field_problems(gemini_worker)
    assert problems["classifier.effort_small"] == ["not a Gemini CLI effort"]
    off = gemini_worker.model_copy(update={"classifier": ClassifierSettings()})
    assert "classifier.effort_small" not in agent_field_problems(off)


def test_credential_problems_name_only_the_providers_own_variable() -> None:
    jev = ClassifierSettings().provider
    decisions = "openai_decisions"
    only_openai = {"openai_api_key": SecretStr("fake-openai-key")}
    assert credential_problems(jev, only_openai) == [
        "Jev: SC_AGENT_CREDENTIALS__TYPESAFE_API_KEY is not set"
    ]
    assert credential_problems(decisions, only_openai) == []
    assert credential_problems(jev, {"typesafe_api_key": SecretStr("  ")}) != []
