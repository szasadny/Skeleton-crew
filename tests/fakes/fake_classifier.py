from collections.abc import Mapping, Sequence

from skeleton_crew.classifiers.base import Answers, ClassifierError
from skeleton_crew.domain.classifier_policy import Choice


class FakeClassifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.answers: dict[str, dict[str, float]] = {}
        self.by_text: dict[str, dict[str, dict[str, float]]] = {}
        self.fail = False

    def answer(self, question: str, **probabilities: float) -> None:
        self.answers[question] = probabilities

    def answer_for(self, needle: str, question: str, **probabilities: float) -> None:
        self.by_text.setdefault(needle, {})[question] = probabilities

    def asked(self, question: str) -> list[str]:
        return [state for state, names in self.calls if question in names]

    async def classify(self, state: str, questions: Sequence[Choice]) -> Answers:
        names = tuple(q.name for q in questions)
        self.calls.append((state, names))
        if self.fail:
            raise ClassifierError("HTTP 529")
        answers: Mapping[str, dict[str, float]] = self.answers
        for needle, specific in self.by_text.items():
            if needle in state:
                answers = {**self.answers, **specific}
        return {name: dict(answers[name]) for name in names if name in answers}
