from dataclasses import dataclass, field
from typing import Self

from skeleton_crew.domain.contest_policy import (
    CheckInput,
    Contest,
    Finding,
    contest,
    finding_violation,
    run_checks,
    uncontested,
)
from skeleton_crew.domain.diff_parse import FileChange, added_lines, parse_diff_summary
from skeleton_crew.domain.diff_policy import check_diff
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.merge_policy import check_resolution, conflict_markers
from skeleton_crew.domain.schemas import WorkerResult
from skeleton_crew.pipeline.outcomes import Escalation
from skeleton_crew.pipeline.texts import (
    COMMIT_AUTHOR,
    UNREADABLE_PATCH,
    commit_message,
    merge_message,
)
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.runtime.controller_git import (
    MAX_PATCH_BYTES,
    CommitSpec,
    agent_patch_argv,
    apply_agent_patch,
    binary_diff,
    diff_summary,
)

PATCH_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class PassedCheck:
    head_sha: str
    patch: bytes
    changes: list[FileChange]
    contested: list[Contest]


@dataclass(frozen=True)
class PolicyCheck:
    head_sha: str | None
    violations: list[str]
    patch: bytes
    changes: list[FileChange] = field(default_factory=list)
    contested: list[Contest] = field(default_factory=list)

    @classmethod
    def rejected(cls, problem: PolicyViolation, patch: bytes, head_sha: str | None = None) -> Self:
        return cls(head_sha, [str(problem)], patch)

    def passed(self) -> PassedCheck | None:
        if self.violations or self.head_sha is None:
            return None
        return PassedCheck(self.head_sha, self.patch, self.changes, self.contested)


@dataclass(frozen=True)
class _Inspection:
    violations: list[str]
    patch: bytes
    changes: list[FileChange] = field(default_factory=list)
    contestable: list[Finding] = field(default_factory=list)

    @classmethod
    def rejected(cls, problem: PolicyViolation, patch: bytes) -> Self:
        return cls([str(problem)], patch)


def commit_spec(parents: tuple[str, ...], message: str) -> CommitSpec:
    return CommitSpec(parents, message, COMMIT_AUTHOR)


async def read_agent_patch(job: WorkerJob) -> bytes:
    result = await job.exec_as_agent(
        agent_patch_argv(job.git.patch_base_sha),
        timeout_s=PATCH_TIMEOUT_S,
        max_stdout_bytes=MAX_PATCH_BYTES + 1,
    )
    if result.exit_code != 0 or result.timed_out:
        raise Escalation(UNREADABLE_PATCH)
    return result.stdout


def _commit(job: WorkerJob, result: WorkerResult) -> CommitSpec:
    git = job.git
    if git.resolving_merge:
        return commit_spec(git.merge_parents, merge_message(job.base_branch, job.branch))
    return commit_spec((git.tip_sha,), commit_message(job.issue.number, result.pr_title))


async def check_change(job: WorkerJob, result: WorkerResult) -> PolicyCheck:
    patch = await read_agent_patch(job)
    repo, git = job.paths.controller_repo, job.git
    try:
        head = await apply_agent_patch(
            repo, patch=patch, base_sha=git.patch_base_sha, commit=_commit(job, result)
        )
    except PolicyViolation as exc:
        return PolicyCheck.rejected(exc, patch)
    found = await _inspect(job, head, patch)
    open_findings, contested = contest(found.contestable, job.progress.objections)
    violations = found.violations + [str(finding_violation(f)) for f in open_findings]
    if git.resolving_merge:
        try:
            violations += await _resolution_violations(job, head)
        except PolicyViolation as exc:
            return PolicyCheck.rejected(exc, found.patch, head)
    return PolicyCheck(head, violations, found.patch, found.changes, contested)


async def _resolution_violations(job: WorkerJob, head: str) -> list[str]:
    git = job.git
    summary = await diff_summary(job.paths.controller_repo, git.patch_base_sha, head)
    touched = [c.path for c in parse_diff_summary(summary.raw, summary.numstat)]
    return [str(v) for v in check_resolution(touched, git.conflicts)]


async def inspect_commit(job: WorkerJob, head: str) -> PolicyCheck:
    found = await _inspect(job, head, b"")
    return PolicyCheck(
        head, found.violations, found.patch, found.changes, uncontested(found.contestable)
    )


async def _inspect(job: WorkerJob, head: str, agent_patch: bytes) -> _Inspection:
    repo, base = job.paths.controller_repo, job.git.base_sha
    is_update = job.on_existing_branch
    # The reviewer of an updated PR needs the whole change, not only the agent's last patch.
    patch = await binary_diff(repo, base, head) if is_update else agent_patch
    summary = await diff_summary(repo, base, head)
    try:
        changes = parse_diff_summary(summary.raw, summary.numstat)
        lines = added_lines(summary.unified)
    except PolicyViolation as exc:
        return _Inspection.rejected(exc, patch)
    settings = job.ctx.settings
    violations = [
        str(v)
        for v in check_diff(changes, settings.repo.protected_paths, settings.limits.max_diff_lines)
    ]
    if agent_patch:
        violations += [
            f"possible secret: {f}" for f in await job.ctx.deps.secret_scanner(agent_patch)
        ]
    if is_update:
        violations += [str(v) for v in conflict_markers(lines)]
    return _Inspection(violations, patch, changes, run_checks(CheckInput(lines, settings)))
