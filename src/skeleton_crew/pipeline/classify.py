import logging
from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.classifiers.base import Answers, ClassifierError
from skeleton_crew.domain.classifier_policy import (
    INJECTION,
    Choice,
    ScreenVerdict,
    injection_problem,
    injection_questions,
    ready_issue_questions,
    screen_verdict,
)
from skeleton_crew.pipeline.candidates import Candidate, Candidates
from skeleton_crew.pipeline.deps import RunContext

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Screened:
    candidates: Candidates
    escalations: dict[int, str]
    efforts: dict[int, str]


def issue_text(candidate: Candidate) -> str:
    issue, comments = candidate.issue, candidate.comments
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


async def _screen(
    ctx: RunContext, candidate: Candidate, questions: Sequence[Choice]
) -> ScreenVerdict:
    answers = await _ask(ctx, issue_text(candidate), questions)
    return screen_verdict(questions, answers, ctx.settings.classifier.routed_efforts)


async def screen_candidates(ctx: RunContext, candidates: Candidates) -> Screened:
    if ctx.classifier is None:
        return Screened(candidates, {}, {})
    chosen = ctx.settings.classifier
    escalations: dict[int, str] = {}
    efforts: dict[int, str] = {}
    for items, questions in (
        (candidates.ready, ready_issue_questions(chosen)),
        (candidates.refine, injection_questions(chosen)),
        ([pr.candidate for pr in candidates.open_prs], injection_questions(chosen)),
    ):
        for item in items:
            verdict = await _screen(ctx, item, questions)
            if verdict.escalation is not None:
                escalations[item.issue.number] = verdict.escalation
            elif verdict.effort is not None:
                efforts[item.issue.number] = verdict.effort
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
