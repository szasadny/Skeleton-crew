import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from skeleton_crew.domain.contest_policy import ObjectionKey
from skeleton_crew.domain.task_state import TaskKind
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.sessions import Sandbox
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.runtime.workspace import TaskPaths


class FollowUpKind(StrEnum):
    CI_FAILED = "ci_failed"
    REVIEW_CHANGES = "review_changes"
    POLICY_FAILED = "policy_failed"
    CHECK_FAILED = "check_failed"


@dataclass(frozen=True)
class FollowUp:
    kind: FollowUpKind
    details: str


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

    def continue_from(self, sha: str) -> None:
        self.patch_base_sha = self.tip_sha = sha

    def resolve_merge(self, conflicted: str, head: str, conflicts: tuple[str, ...]) -> None:
        self.patch_base_sha, self.tip_sha = conflicted, head
        self.merge_parents, self.conflicts = (head, self.base_sha), conflicts

    def committed(self, sha: str) -> None:
        self.tip_sha, self.merge_parents = sha, ()

    @property
    def resolving_merge(self) -> bool:
        return bool(self.merge_parents)


@dataclass
class Rounds:
    check: int = 0
    ci: int = 0
    review: int = 0
    policy: int = 0
    new_issues: int = 0


@dataclass
class ReviewerState:
    sandbox: Sandbox | None = None
    session_id: str | None = None
    reviewed_sha: str | None = None


@dataclass
class WorkerJob:
    ctx: RunContext
    record: WorkerTaskRecord
    issue: IssueRef
    comments: Sequence[IssueComment]
    key: str
    paths: TaskPaths
    branch: str
    kind: TaskKind = TaskKind.ISSUE
    effort: str | None = None
    continues_branch: bool = False
    pr_requested: bool = False
    git: GitState = field(default_factory=GitState)
    rounds: Rounds = field(default_factory=Rounds)
    sandbox: Sandbox | None = None
    session_id: str | None = None
    objections: dict[ObjectionKey, str] = field(default_factory=dict)
    reviewer: ReviewerState = field(default_factory=ReviewerState)

    @property
    def on_existing_branch(self) -> bool:
        return self.kind is not TaskKind.ISSUE or self.continues_branch


def review_root(paths: TaskPaths) -> Path:
    return paths.root / "review"


def remove_work_dirs(paths: TaskPaths) -> None:
    for path in (paths.agent_repo, paths.inbox, paths.controller_repo, review_root(paths)):
        shutil.rmtree(path, ignore_errors=True)
