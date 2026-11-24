from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome, AuditSink
from skeleton_crew.domain.candidate_policy import PrRoute, pr_route, wants_refine, wants_work
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import Gateway, IssueRef, PullRef
from skeleton_crew.github.trust import trusted_issues
from skeleton_crew.pipeline.deps import PipelineDeps


@dataclass(frozen=True)
class Candidate:
    issue: IssueRef
    comments: Sequence[IssueComment]


@dataclass(frozen=True)
class OpenPr:
    candidate: Candidate
    pull: PullRef

    @property
    def issue(self) -> IssueRef:
        return self.candidate.issue


@dataclass(frozen=True)
class Candidates:
    ready: list[Candidate]
    refine: list[Candidate]
    open_prs: list[OpenPr]


async def find_candidates(
    deps: PipelineDeps, settings: AppSettings, gw: Gateway, audit: AuditSink
) -> Candidates:
    labels, trusted = gw.labels, settings.repo.trusted_users
    bot = deps.config.github_bot_login
    ready: list[Candidate] = []
    open_prs: list[OpenPr] = []
    pulls_by_issue = await gw.open_skeleton_prs()
    for issue, comments in await trusted_issues(gw, labels.ready, trusted, bot):
        if not wants_work(issue.labels, labels):
            continue
        pulls = pulls_by_issue.get(issue.number, ())
        match pr_route(len(pulls)):
            case PrRoute.NEW_WORK:
                ready.append(Candidate(issue, comments))
            case PrRoute.MAINTENANCE:
                open_prs.append(OpenPr(Candidate(issue, comments), pulls[0]))
            case PrRoute.AMBIGUOUS:
                await _skip_ambiguous(audit, issue.number, pulls)
    refine = [
        Candidate(issue, comments)
        for issue, comments in await trusted_issues(gw, labels.refine, trusted, bot)
        if wants_refine(issue.labels, labels)
    ]
    return Candidates(ready, refine, open_prs)


async def _skip_ambiguous(audit: AuditSink, issue_number: int, pulls: Sequence[PullRef]) -> None:
    numbers = sorted(pull.number for pull in pulls)
    await audit(
        AuditEntry(
            AuditKind.ISSUE_SKIPPED,
            AuditOutcome.INFO,
            f"#{issue_number} has {len(numbers)} open skeleton PRs",
            {"issue": issue_number, "prs": numbers},
        )
    )
