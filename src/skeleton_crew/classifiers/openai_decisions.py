from collections.abc import Sequence

from pydantic import BaseModel

from skeleton_crew.classifiers.base import Answers, ClassifierError, ClassifierSpec, HttpClassifier
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


class DecisionsClassifier(HttpClassifier[_Response]):
    url = URL
    response = _Response

    def _body(self, state: str, questions: Sequence[Choice]) -> dict[str, object]:
        return {
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

    def _answers(self, reply: _Response) -> Answers:
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
