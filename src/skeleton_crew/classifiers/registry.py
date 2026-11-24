from collections.abc import Mapping

from pydantic import SecretStr

from skeleton_crew.classifiers import jev, openai_decisions
from skeleton_crew.classifiers.base import Classifier, ClassifierSpec
from skeleton_crew.core.config import credential_setting, secret_value
from skeleton_crew.domain.settings import AppSettings, ClassifierSettings, problem_lines

_PROVIDERS: Mapping[str, ClassifierSpec] = {
    spec.name: spec for spec in (jev.SPEC, openai_decisions.SPEC)
}


def available_classifiers() -> tuple[ClassifierSpec, ...]:
    return tuple(_PROVIDERS.values())


def field_problems(settings: AppSettings) -> dict[str, list[str]]:
    chosen = settings.classifier
    spec = _PROVIDERS.get(chosen.provider)
    if spec is None:
        return {"classifier.provider": [f"unknown classifier {chosen.provider}"]}
    if chosen.model not in spec.models:
        return {"classifier.model": [f"not a {spec.display_name} model"]}
    return {}


def settings_problems(settings: AppSettings) -> list[str]:
    return problem_lines(field_problems(settings))


def credential_problems(provider: str, credentials: Mapping[str, SecretStr]) -> list[str]:
    spec = _PROVIDERS[provider]
    if secret_value(credentials.get(spec.credential)) is None:
        return [f"{spec.display_name}: {credential_setting(spec.credential)} is not set"]
    return []


def classifier_for(
    settings: ClassifierSettings, credentials: Mapping[str, SecretStr]
) -> Classifier:
    spec = _PROVIDERS[settings.provider]
    # A provider only ever sees its own credential.
    return spec.factory(credentials.get(spec.credential), settings.model)
