from collections.abc import Sequence

from pydantic import BaseModel, SecretStr

from skeleton_crew.classifiers.base import (
    Answers,
    ClassifierError,
    ClassifierSpec,
    post_json,
)
from skeleton_crew.domain.classifier_policy import Choice

URL = "https://api.openai.com/v1/decisions"


class _Probability(BaseModel):
    value: str
    probability: float


class _Answer(BaseModel):
    type: str
    name: str
    probabilities: list[_Probability] = []


class _Response(BaseModel):
    answers: list[_Answer]


class DecisionsClassifier:
    def __init__(self, api_key: SecretStr | None, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def classify(self, state: str, questions: Sequence[Choice]) -> Answers:
        body = {
            "model": self._model,
            "input": state,
            "questions": [
                {
                    "type": "choice",
                    "name": q.name,
                    "instructions": q.instructions,
                    "choices": [{"value": v, "description": d} for v, d in q.options.items()],
                }
                for q in questions
            ],
        }
        reply = await post_json(URL, self._api_key, body, _Response)
        if any(answer.type != "choice" for answer in reply.answers):
            raise ClassifierError("the classifier refused a question")
        return {a.name: {p.value: p.probability for p in a.probabilities} for a in reply.answers}


SPEC = ClassifierSpec(
    name="openai_decisions",
    display_name="OpenAI Decisions",
    models=("gpt-6-luna",),
    credential="openai_api_key",
    factory=DecisionsClassifier,
)
