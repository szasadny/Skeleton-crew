from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from skeleton_crew.domain.branch_policy import branch_name
from skeleton_crew.domain.contest_policy import ObjectionKey
from skeleton_crew.domain.schemas import WorkerResult
from skeleton_crew.domain.task_keys import worker_key
from skeleton_crew.domain.task_state import (
    ROUND_RULES,
    FollowUpKind,
    JobMode,
    rounds_exhausted,
    works_on_existing_branch,
)
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.pipeline.candidates import Candidate, OpenPr
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.outcomes import Escalation
from skeleton_crew.pipeline.sandbox import Sandbox
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.pipeline.texts import CONTINUED_SUMMARY
from skeleton_crew.runtime.containers import DEFAULT_MAX_STDOUT_BYTES, ExecRequest, ExecResult
from skeleton_crew.runtime.task_dirs import TaskPaths


@dataclass(frozen=True)
class FollowUp:
    kind: FollowUpKind
    details: str


@dataclass(frozen=True)
class WorkerReport:
    title: str
    summary: str
    blockers: Sequence[str]

    @classmethod
    def of(cls, result: WorkerResult) -> Self:
        return cls(result.pr_title, result.summary, result.blockers)


@dataclass
class GitState:
    base_sha: str = ""
    patch_base_sha: str = ""
    tip_sha: str = ""
    remote_sha: str | None = None
    merge_parents: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def start_at(self, sha: str) -> None:
        self.base_sha = self.patch_base_sha = self.tip_sha = sha

    def merged(self, base: str, sha: str) -> None:
        self.base_sha = base
        self.patch_base_sha = self.tip_sha = sha

    def resolve_merge(
        self, conflicted: str, head: str, base: str, conflicts: tuple[str, ...]
    ) -> None:
        self.base_sha = base
        self.patch_base_sha, self.tip_sha = conflicted, head
        self.merge_parents, self.conflicts = (head, base), conflicts

    def committed(self, sha: str) -> None:
        self.tip_sha, self.merge_parents = sha, ()

    @property
    def resolving_merge(self) -> bool:
        return bool(self.merge_parents)


@dataclass
class ReviewerState:
    sandbox: Sandbox | None = None
    session_id: str | None = None
    reviewed_sha: str | None = None


@dataclass
class JobProgress:
    mode: JobMode = JobMode.FRESH
    pr_requested: bool = False
    sandbox: Sandbox | None = None
    session_id: str | None = None
    objections: dict[ObjectionKey, str] = field(default_factory=dict)
    rounds: Counter[FollowUpKind] = field(default_factory=Counter)
    new_issues: int = 0


@dataclass(frozen=True)
class WorkerJob:
    ctx: RunContext
    record: WorkerTaskRecord
    issue: IssueRef
    comments: Sequence[IssueComment]
    branch: str
    effort: str | None = None
    git: GitState = field(default_factory=GitState)
    reviewer: ReviewerState = field(default_factory=ReviewerState)
    progress: JobProgress = field(default_factory=JobProgress)

    @classmethod
    def for_issue(
        cls,
        ctx: RunContext,
        record: WorkerTaskRecord,
        candidate: Candidate,
        effort: str | None,
    ) -> Self:
        issue = candidate.issue
        branch = branch_name(issue.number, issue.title)
        return cls(ctx, record, issue, candidate.comments, branch, effort)

    @classmethod
    def for_pr(cls, ctx: RunContext, record: WorkerTaskRecord, pr: OpenPr) -> Self:
        return cls(
            ctx,
            record,
            pr.issue,
            pr.candidate.comments,
            pr.pull.head_ref,
            git=GitState(remote_sha=pr.pull.head_sha),
            progress=JobProgress(mode=JobMode.PR_FIX, pr_requested=True),
        )

    @property
    def key(self) -> str:
        return worker_key(self.record.task_id)

    @property
    def paths(self) -> TaskPaths:
        return self.ctx.task_paths(self.key)

    @property
    def base_branch(self) -> str:
        return self.ctx.settings.repo.base_branch

    @property
    def on_existing_branch(self) -> bool:
        return works_on_existing_branch(self.progress.mode)

    def continued_report(self) -> WorkerReport:
        return WorkerReport(self.issue.title, CONTINUED_SUMMARY, ())

    def require_sandbox(self) -> Sandbox:
        if self.progress.sandbox is None:
            raise RuntimeError("worker container is not running")
        return self.progress.sandbox

    async def exec_as_agent(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        stdout_path: Path | None = None,
        max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
    ) -> ExecResult:
        request = ExecRequest.as_agent(
            argv, timeout_s=timeout_s, stdout_path=stdout_path, max_stdout_bytes=max_stdout_bytes
        )
        return await self.ctx.deps.runtime.exec(self.require_sandbox().handle, request)


async def next_round(job: WorkerJob, kind: FollowUpKind) -> None:
    rounds = job.progress.rounds
    rounds[kind] += 1
    exhausted = rounds_exhausted(kind, rounds[kind], job.ctx.settings.limits)
    if exhausted is not None:
        raise Escalation(exhausted)
    rule = ROUND_RULES[kind]
    await job.record.fire(rule.event, **{rule.counter_field: rounds[kind]})
