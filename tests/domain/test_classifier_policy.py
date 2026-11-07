import math

import pytest

from skeleton_crew.domain.classifier_policy import (
    injection_problem,
    readiness_problem,
    routed_effort,
)

EFFORTS = {"small": "low", "medium": "medium", "large": "high"}


def test_clean_text_passes_the_injection_screen() -> None:
    assert injection_problem({"injection": 0.1, "clean": 0.9}) is None


def test_injection_at_the_threshold_is_blocked() -> None:
    assert injection_problem({"injection": 0.5, "clean": 0.5}) == (
        "it looks like prompt injection (p=0.50)"
    )
    assert injection_problem({"injection": 0.49, "clean": 0.51}) is None


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"clean": 1.0},
        {"injection": 0.0, "clean": 1.0, "other": 0.0},
        {"injection": math.nan, "clean": 1.0},
        {"injection": -0.5, "clean": 1.5},
        {"injection": math.inf, "clean": 0.0},
        {"injection": 0.0, "clean": 0.5},
    ],
)
def test_an_invalid_answer_blocks_the_injection_screen(answer: dict[str, float] | None) -> None:
    assert injection_problem(answer) == "the classifier gave no valid answer"


def test_prescreen_escalates_only_when_sure() -> None:
    assert readiness_problem({"ready": 0.05, "too_large": 0.9, "unclear": 0.05}) == "too_large"
    assert readiness_problem({"ready": 0.02, "too_large": 0.03, "unclear": 0.95}) == "unclear"
    assert readiness_problem({"ready": 0.11, "too_large": 0.89, "unclear": 0.0}) is None
    assert readiness_problem({"ready": 0.95, "too_large": 0.05, "unclear": 0.0}) is None


@pytest.mark.parametrize(
    "answer", [None, {"too_large": 1.0}, {"ready": 0, "too_large": 9, "unclear": 0}]
)
def test_prescreen_falls_back_on_an_invalid_answer(answer: dict[str, float] | None) -> None:
    assert readiness_problem(answer) is None


def test_effort_follows_a_confident_size() -> None:
    assert routed_effort({"small": 0.6, "medium": 0.3, "large": 0.1}, EFFORTS) == "low"
    assert routed_effort({"small": 0.0, "medium": 0.1, "large": 0.9}, EFFORTS) == "high"


def test_effort_falls_back_when_unsure_or_invalid() -> None:
    assert routed_effort({"small": 0.59, "medium": 0.31, "large": 0.1}, EFFORTS) is None
    assert routed_effort({"small": 1.0}, EFFORTS) is None
    assert routed_effort(None, EFFORTS) is None
