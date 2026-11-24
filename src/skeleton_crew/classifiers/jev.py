from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from skeleton_crew.classifiers.base import Answers, ClassifierSpec, HttpClassifier
from skeleton_crew.domain.classifier_policy import Choice

URL = "https://api.typesafe.ai/v1/systemone"


class _Answer(BaseModel):
    type: Literal["choice"]
    probabilities: dict[str, float]


class _Response(BaseModel):
    answers: dict[str, _Answer]


class JevClassifier(HttpClassifier[_Response]):
    url = URL
    response = _Response

    def _body(self, state: str, questions: Sequence[Choice]) -> dict[str, object]:
        return {
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

    def _answers(self, reply: _Response) -> Answers:
        return {name: answer.probabilities for name, answer in reply.answers.items()}


SPEC = ClassifierSpec(
    name="jev",
    display_name="Jev",
    models=("jev-latest", "jev-1.13.0"),
    credential="typesafe_api_key",
    factory=JevClassifier,
)
