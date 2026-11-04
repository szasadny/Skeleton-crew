import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from skeleton_crew.domain.contest_policy import ObjectionKey
from skeleton_crew.domain.task_state import TaskKind
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import IssueRef
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.sessions import Sandbox
from skeleton_crew.pipeline.task_record import WorkerTaskRecord
from skeleton_crew.runtime.workspace import TaskPaths


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
    base_sha: str = ""
    patch_base_sha: str = ""
    tip_sha: str = ""
    remote_sha: str | None = None
    merge_parents: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    sandbox: Sandbox | None = None
    session_id: str | None = None
    pr_requested: bool = False
    new_issues: int = 0
    ci_rounds: int = 0
    review_rounds: int = 0
    policy_rounds: int = 0
    objections: dict[ObjectionKey, str] = field(default_factory=dict)


def review_root(paths: TaskPaths) -> Path:
    return paths.root / "review"


def remove_work_dirs(paths: TaskPaths) -> None:
    for path in (paths.agent_repo, paths.inbox, paths.controller_repo, review_root(paths)):
        shutil.rmtree(path, ignore_errors=True)
