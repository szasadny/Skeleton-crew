from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, SecretStr

from skeleton_crew.classifiers.base import (
    Answers,
    ClassifierSpec,
    post_json,
)
from skeleton_crew.domain.classifier_policy import Choice

URL = "https://api.typesafe.ai/v1/systemone"


class _Answer(BaseModel):
    type: Literal["choice"]
    probabilities: dict[str, float]


class _Response(BaseModel):
    answers: dict[str, _Answer]


class JevClassifier:
    def __init__(self, api_key: SecretStr | None, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def classify(self, state: str, questions: Sequence[Choice]) -> Answers:
        body = {
            "model": self._model,
            "state": state,
            "questions": {
                q.name: {
                    "type": "choice",
                    "instructions": q.instructions,
                    "criteria": dict(q.options),
                }
                for q in questions
            },
        }
        reply = await post_json(URL, self._api_key, body, _Response)
        return {name: answer.probabilities for name, answer in reply.answers.items()}


SPEC = ClassifierSpec(
    name="jev",
    display_name="Jev",
    models=("jev-latest", "jev-1.13.0"),
    credential="typesafe_api_key",
    factory=JevClassifier,
)
