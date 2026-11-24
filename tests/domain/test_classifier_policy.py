import math

import pytest

from skeleton_crew.domain.classifier_policy import (
    INJECTION,
    READINESS,
    SIZE,
    ScreenVerdict,
    injection_problem,
    injection_questions,
    injection_reason,
    readiness_problem,
    ready_issue_questions,
    routed_effort,
    screen_verdict,
)
from skeleton_crew.domain.settings import ClassifierSettings

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


ALL_ON = ClassifierSettings(injection_screen=True, prescreen=True, effort_routing=True)
INJECTED = {"injection": 0.97, "clean": 0.03}
CLEAN = {"injection": 0.02, "clean": 0.98}
TOO_LARGE = {"ready": 0.02, "too_large": 0.95, "unclear": 0.03}
SMALL = {"small": 0.8, "medium": 0.15, "large": 0.05}


def test_ready_issues_get_every_enabled_question_in_a_fixed_order() -> None:
    assert ready_issue_questions(ALL_ON) == [INJECTION, READINESS, SIZE]
    only_size = ClassifierSettings(effort_routing=True)
    assert ready_issue_questions(only_size) == [SIZE]
    assert ready_issue_questions(ClassifierSettings()) == []


def test_other_candidates_only_get_the_injection_screen() -> None:
    assert injection_questions(ALL_ON) == [INJECTION]
    assert injection_questions(ClassifierSettings(prescreen=True, effort_routing=True)) == []


def test_injection_reason_keeps_its_text() -> None:
    assert injection_reason("issue text", "it looks like prompt injection (p=0.97)") == (
        "the injection screen stopped the issue text: it looks like prompt injection (p=0.97)."
        " Edit the text to go on"
    )


def test_an_injection_hit_escalates_before_anything_else() -> None:
    answers = {"injection": INJECTED, "readiness": TOO_LARGE, "size": SMALL}
    verdict = screen_verdict([INJECTION, READINESS, SIZE], answers, EFFORTS)
    assert verdict == ScreenVerdict(
        escalation=injection_reason("issue text", "it looks like prompt injection (p=0.97)")
    )


def test_a_missing_injection_answer_fails_closed() -> None:
    verdict = screen_verdict([INJECTION], {}, EFFORTS)
    assert verdict.escalation == injection_reason(
        "issue text", "the classifier gave no valid answer"
    )


def test_a_confident_prescreen_escalates_with_its_reason() -> None:
    answers = {"injection": CLEAN, "readiness": TOO_LARGE}
    verdict = screen_verdict([INJECTION, READINESS], answers, EFFORTS)
    assert verdict == ScreenVerdict(
        escalation="the pre-screen judged the issue too large for one pull request"
    )


def test_a_clean_ready_issue_gets_its_routed_effort() -> None:
    answers = {"injection": CLEAN, "size": SMALL}
    assert screen_verdict([INJECTION, SIZE], answers, EFFORTS) == ScreenVerdict(effort="low")


def test_unasked_questions_decide_nothing() -> None:
    answers = {"readiness": TOO_LARGE, "size": SMALL}
    assert screen_verdict([], answers, EFFORTS) == ScreenVerdict()
