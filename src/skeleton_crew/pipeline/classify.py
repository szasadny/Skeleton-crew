import logging
from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.classifiers.base import Answers, ClassifierError
from skeleton_crew.domain.classifier_policy import (
    INJECTION,
    READINESS,
    SIZE,
    Choice,
    injection_problem,
    readiness_problem,
    routed_effort,
)
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.run_setup import Candidates

_READINESS_REASONS = {
    "too_large": "the pre-screen judged the issue too large for one pull request",
    "unclear": "the pre-screen judged the issue unclear: it needs more detail or an owner decision",
}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Screened:
    candidates: Candidates
    escalations: dict[int, str]
    efforts: dict[int, str]


def injection_reason(source: str, problem: str) -> str:
    return f"the injection screen stopped the {source}: {problem}. Edit the text to go on"


def issue_text(issue: IssueRef, comments: Sequence[IssueComment]) -> str:
    parts = [issue.title, issue.body, *(f"Comment by {c.author}:\n{c.body}" for c in comments)]
    return "\n\n".join(parts)


async def _ask(ctx: RunContext, state: str, questions: Sequence[Choice]) -> Answers:
    if ctx.classifier is None or not questions:
        return {}
    try:
        return await ctx.classifier.classify(state, questions)
    except ClassifierError as exc:
        log.warning("classifier failed: %s", exc)
        return {}


async def _screen_issue(
    ctx: RunContext, issue: IssueRef, comments: Sequence[IssueComment], *, ready: bool
) -> tuple[str | None, str | None]:
    chosen = ctx.settings.classifier
    wanted = (
        (INJECTION, chosen.injection_screen),
        (READINESS, ready and chosen.prescreen),
        (SIZE, ready and chosen.effort_routing),
    )
    questions = [question for question, on in wanted if on]
    answers = await _ask(ctx, issue_text(issue, comments), questions)
    if chosen.injection_screen and (problem := injection_problem(answers.get(INJECTION.name))):
        return injection_reason("issue text", problem), None
    if READINESS in questions and (verdict := readiness_problem(answers.get(READINESS.name))):
        return _READINESS_REASONS[verdict], None
    if SIZE in questions:
        return None, routed_effort(answers.get(SIZE.name), chosen.routed_efforts)
    return None, None


async def screen_candidates(ctx: RunContext, candidates: Candidates) -> Screened:
    if ctx.classifier is None:
        return Screened(candidates, {}, {})
    escalations: dict[int, str] = {}
    efforts: dict[int, str] = {}
    for ready, items in (
        (True, candidates.ready),
        (False, candidates.refine),
        (False, candidates.open_prs),
    ):
        for item in items:
            number = item.issue.number
            reason, effort = await _screen_issue(ctx, item.issue, item.comments, ready=ready)
            if reason is not None:
                escalations[number] = reason
            elif effort is not None:
                efforts[number] = effort
    kept = Candidates(
        ready=[c for c in candidates.ready if c.issue.number not in escalations],
        refine=[c for c in candidates.refine if c.issue.number not in escalations],
        open_prs=[p for p in candidates.open_prs if p.issue.number not in escalations],
    )
    return Screened(kept, escalations, efforts)


async def screen_output(ctx: RunContext, text: str) -> str | None:
    if not ctx.settings.classifier.injection_screen:
        return None
    answers = await _ask(ctx, text, [INJECTION])
    return injection_problem(answers.get(INJECTION.name))
