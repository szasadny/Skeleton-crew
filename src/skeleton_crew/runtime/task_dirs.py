import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from skeleton_crew.domain.task_keys import is_task_key

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskPaths:
    root: Path
    controller_repo: Path
    agent_repo: Path
    inbox: Path
    transcripts: Path


def task_paths(tasks_dir: Path, task_key: str) -> TaskPaths:
    if not is_task_key(task_key):
        raise ValueError(f"invalid task key: {task_key!r}")
    root = tasks_dir / task_key
    return TaskPaths(
        root=root,
        controller_repo=root / "controller",
        agent_repo=root / "agent",
        inbox=root / "inbox",
        transcripts=root / "transcripts",
    )


def list_task_keys(tasks_dir: Path) -> list[str]:
    if tasks_dir.is_symlink() or not tasks_dir.is_dir():
        return []
    return sorted(
        child.name
        for child in tasks_dir.iterdir()
        if is_task_key(child.name) and not child.is_symlink() and child.is_dir()
    )


def remove_task_dirs(tasks_dir: Path, keys: Iterable[str]) -> list[str]:
    # Without fd-based rmtree a swapped-in symlink could redirect the delete outside tasks_dir.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError("rmtree is not symlink-safe on this platform")
    root = tasks_dir.resolve()
    removed = []
    for key in keys:
        path = root / key
        if not is_task_key(key) or path.is_symlink() or not path.is_dir():
            continue
        if path.resolve().parent != root:
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            log.exception("could not remove task directory %s", key)
            continue
        removed.append(key)
    return removed
