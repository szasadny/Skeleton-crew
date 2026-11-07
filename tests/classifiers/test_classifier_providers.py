import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from skeleton_crew.classifiers import jev, openai_decisions
from skeleton_crew.classifiers.base import ClassifierError, ClassifierSpec
from skeleton_crew.domain.classifier_policy import INJECTION, SIZE

KEY = "fake-classifier-key-1234"
SPECS = [jev.SPEC, openai_decisions.SPEC]
URLS = {jev.SPEC.name: jev.URL, openai_decisions.SPEC.name: openai_decisions.URL}


def _jev_reply() -> dict[str, object]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "injection": {
                "type": "choice",
                "choice": "clean",
                "confidence": 0.8,
                "probabilities": {"injection": 0.1, "clean": 0.9},
            }
        },
        "usage": {"input_tokens": 12, "output_tokens": 0},
    }


def _decisions_reply() -> dict[str, object]:
    return {
        "answers": [
            {
                "type": "choice",
                "name": "injection",
                "choice": "clean",
                "confidence": 0.8,
                "probabilities": [
                    {"value": "injection", "probability": 0.1},
                    {"value": "clean", "probability": 0.9},
                ],
            }
        ]
    }


@respx.mock
async def test_jev_sends_choices_as_criteria_with_the_bearer_key() -> None:
    route = respx.post(jev.URL).mock(return_value=httpx.Response(200, json=_jev_reply()))
    answers = await jev.JevClassifier(SecretStr(KEY), "jev-latest").classify(
        "some text", [INJECTION, SIZE]
    )
    assert answers == {"injection": {"injection": 0.1, "clean": 0.9}}
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest"
    assert body["state"] == "some text"
    assert body["questions"]["size"] == {
        "type": "choice",
        "instructions": SIZE.instructions,
        "criteria": dict(SIZE.options),
    }


@respx.mock
async def test_decisions_sends_choices_as_a_list_with_the_bearer_key() -> None:
    route = respx.post(openai_decisions.URL).mock(
        return_value=httpx.Response(200, json=_decisions_reply())
    )
    classifier = openai_decisions.DecisionsClassifier(SecretStr(KEY), "gpt-6-luna")
    answers = await classifier.classify("some text", [INJECTION])
    assert answers == {"injection": {"injection": 0.1, "clean": 0.9}}
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6-luna"
    assert body["input"] == "some text"
    assert body["questions"][0]["name"] == "injection"
    assert {"value": "clean", "description": INJECTION.options["clean"]} in body["questions"][0][
        "choices"
    ]


@respx.mock
async def test_a_refusal_is_an_error() -> None:
    respx.post(openai_decisions.URL).mock(
        return_value=httpx.Response(200, json={"answers": [{"type": "refusal", "name": "size"}]})
    )
    classifier = openai_decisions.DecisionsClassifier(SecretStr(KEY), "gpt-6-luna")
    with pytest.raises(ClassifierError, match="refused"):
        await classifier.classify("text", [SIZE])


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
@pytest.mark.parametrize("status", [400, 401, 422, 429, 500, 529])
@respx.mock
async def test_an_error_status_is_an_error_without_the_key(
    spec: ClassifierSpec, status: int
) -> None:
    respx.post(URLS[spec.name]).mock(
        return_value=httpx.Response(status, json={"detail": f"bad key {KEY}"})
    )
    with pytest.raises(ClassifierError) as caught:
        await spec.factory(SecretStr(KEY), spec.models[0]).classify("text", [INJECTION])
    assert str(caught.value) == f"HTTP {status}"


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
@pytest.mark.parametrize("content", [b"not json", b"{}", b'{"answers": 5}', b"[]"])
@respx.mock
async def test_an_unexpected_body_is_an_error(spec: ClassifierSpec, content: bytes) -> None:
    respx.post(URLS[spec.name]).mock(return_value=httpx.Response(200, content=content))
    with pytest.raises(ClassifierError, match="unexpected response"):
        await spec.factory(SecretStr(KEY), spec.models[0]).classify("text", [INJECTION])


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
@respx.mock
async def test_a_transport_error_is_an_error_without_details(spec: ClassifierSpec) -> None:
    respx.post(URLS[spec.name]).mock(side_effect=httpx.ConnectError(f"refused {KEY}"))
    with pytest.raises(ClassifierError) as caught:
        await spec.factory(SecretStr(KEY), spec.models[0]).classify("text", [INJECTION])
    assert str(caught.value) == "request failed: ConnectError"


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
@pytest.mark.parametrize("key", [None, "", "   "])
@respx.mock
async def test_no_request_without_a_key(spec: ClassifierSpec, key: str | None) -> None:
    route = respx.post(URLS[spec.name])
    with pytest.raises(ClassifierError, match="no API key"):
        await spec.factory(SecretStr(key) if key is not None else None, spec.models[0]).classify(
            "text", [INJECTION]
        )
    assert not route.called
