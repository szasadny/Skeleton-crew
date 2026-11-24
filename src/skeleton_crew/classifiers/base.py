from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from skeleton_crew.core.config import secret_value
from skeleton_crew.domain.classifier_policy import Choice

TIMEOUT_S = 20.0

type Answers = dict[str, dict[str, float]]


class ClassifierError(Exception):
    pass


class Classifier(Protocol):
    async def classify(self, state: str, questions: Sequence[Choice]) -> Answers: ...


type ClassifierFactory = Callable[[SecretStr | None, str], Classifier]


@dataclass(frozen=True)
class ClassifierSpec:
    name: str
    display_name: str
    models: tuple[str, ...]
    credential: str
    factory: ClassifierFactory


async def post_json[T: BaseModel](
    url: str, api_key: SecretStr | None, body: object, response: type[T]
) -> T:
    key = secret_value(api_key)
    if key is None:
        raise ClassifierError("no API key")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            reply = await client.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        # Only the type: an exception text could echo request details.
        raise ClassifierError(f"request failed: {type(exc).__name__}") from None
    if reply.status_code != httpx.codes.OK:
        raise ClassifierError(f"HTTP {reply.status_code}")
    try:
        return response.model_validate_json(reply.content)
    except ValidationError:
        raise ClassifierError("unexpected response") from None


class HttpClassifier[R: BaseModel](ABC):
    url: ClassVar[str]
    response: type[R]

    def __init__(self, api_key: SecretStr | None, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def classify(self, state: str, questions: Sequence[Choice]) -> Answers:
        body = self._body(state, questions)
        return self._answers(await post_json(self.url, self._api_key, body, self.response))

    @abstractmethod
    def _body(self, state: str, questions: Sequence[Choice]) -> dict[str, object]: ...

    @abstractmethod
    def _answers(self, reply: R) -> Answers: ...
