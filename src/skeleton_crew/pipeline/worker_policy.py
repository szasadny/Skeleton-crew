from dataclasses import dataclass

from skeleton_crew.domain.contest_policy import (
    CheckInput,
    Contest,
    Finding,
    contest,
    uncontested,
    violation_text,
)
from skeleton_crew.domain.contestable_checks import run_checks
from skeleton_crew.domain.diff_lines import added_lines
from skeleton_crew.domain.diff_policy import FileChange, check_diff, parse_diff_summary
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.merge_policy import check_resolution, conflict_markers
from skeleton_crew.domain.schemas import WorkerResult
from skeleton_crew.pipeline.escalation import Escalation
from skeleton_crew.pipeline.sessions import AGENT_USER, WORKSPACE
from skeleton_crew.pipeline.texts import COMMIT_AUTHOR, commit_message, merge_message
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.runtime.workspace import (
    MAX_PATCH_BYTES,
    agent_patch_argv,
    apply_agent_patch,
    binary_diff,
    diff_summary,
)

PATCH_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class PolicyCheck:
    head_sha: str | None
    violations: list[str]
    patch: bytes
    changes: list[FileChange]
    contested: list[Contest]


@dataclass(frozen=True)
class _Inspection:
    violations: list[str]
    patch: bytes
    changes: list[FileChange]
    contestable: list[Finding]


async def read_agent_patch(job: WorkerJob) -> bytes:
    if job.sandbox is None:
        raise RuntimeError("worker container is not running")
    result = await job.ctx.deps.runtime.exec(
        job.sandbox.handle,
        agent_patch_argv(job.patch_base_sha),
        env={},
        cwd=WORKSPACE,
        user=AGENT_USER,
        timeout_s=PATCH_TIMEOUT_S,
        max_stdout_bytes=MAX_PATCH_BYTES + 1,
    )
    if result.exit_code != 0 or result.timed_out:
        raise Escalation("could not read the worker's changes from its workspace")
    return result.stdout


async def check_change(job: WorkerJob, result: WorkerResult) -> PolicyCheck:
    patch = await read_agent_patch(job)
    repo = job.paths.controller_repo
    resolving = bool(job.merge_parents)
    message = (
        merge_message(job.ctx.settings.repo.base_branch, job.branch)
        if resolving
        else commit_message(job.issue.number, result.pr_title)
    )
    try:
        head = await apply_agent_patch(
            repo,
            patch=patch,
            base_sha=job.patch_base_sha,
            parent_sha=job.merge_parents[0] if resolving else job.tip_sha,
            message=message,
            author=COMMIT_AUTHOR,
            extra_parents=job.merge_parents[1:],
        )
    except PolicyViolation as exc:
        return PolicyCheck(None, [str(exc)], patch, [], [])
    found = await _inspect(job, head, patch)
    open_findings, contested = contest(found.contestable, job.objections)
    violations = found.violations + [violation_text(f) for f in open_findings]
    if resolving:
        raw, numstat, _ = await diff_summary(repo, job.patch_base_sha, head)
        try:
            touched = [c.path for c in parse_diff_summary(raw, numstat)]
        except PolicyViolation as exc:
            return PolicyCheck(head, [str(exc)], found.patch, [], [])
        violations += check_resolution(touched, job.conflicts)
    return PolicyCheck(head, violations, found.patch, found.changes, contested)


async def inspect_commit(job: WorkerJob, head: str, agent_patch: bytes) -> PolicyCheck:
    found = await _inspect(job, head, agent_patch)
    return PolicyCheck(
        head, found.violations, found.patch, found.changes, uncontested(found.contestable)
    )


async def _inspect(job: WorkerJob, head: str, agent_patch: bytes) -> _Inspection:
    repo = job.paths.controller_repo
    is_update = job.on_existing_branch
    # The reviewer of an updated PR needs the whole change, not only the agent's last patch.
    patch = await binary_diff(repo, job.base_sha, head) if is_update else agent_patch
    raw, numstat, unified = await diff_summary(repo, job.base_sha, head)
    try:
        changes = parse_diff_summary(raw, numstat)
        lines = added_lines(unified)
    except PolicyViolation as exc:
        return _Inspection([str(exc)], patch, [], [])
    settings = job.ctx.settings
    violations = [
        f"{v.path}: {v.reason}"
        for v in check_diff(changes, settings.repo.protected_paths, settings.limits.max_diff_lines)
    ]
    if agent_patch:
        violations += [
            f"possible secret: {f}" for f in await job.ctx.deps.secret_scanner(agent_patch)
        ]
    if is_update:
        violations += conflict_markers(lines)
    contestable = run_checks(CheckInput(changes, lines, settings))
    return _Inspection(violations, patch, changes, contestable)
