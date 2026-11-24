import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from skeleton_crew.core.git import run_git_sync
from skeleton_crew.domain.diff_parse import REGULAR_FILE_MODES
from skeleton_crew.domain.repo_docs import (
    MAX_CANDIDATES,
    MAX_DOC_BYTES,
    instruction_paths,
    linked_paths,
)
from skeleton_crew.runtime.controller_git import LOCAL_GIT_TIMEOUT_S


@dataclass(frozen=True)
class DocSources:
    candidates: tuple[str, ...]
    texts: dict[str, str]


def _regular_tracked(repo: Path) -> set[str]:
    listing = run_git_sync(("ls-files", "--stage", "-z"), cwd=repo, timeout_s=LOCAL_GIT_TIMEOUT_S)
    found = set()
    for entry in listing.decode(errors="surrogateescape").split("\0"):
        info, _, path = entry.partition("\t")
        if info.split(" ", 1)[0] in REGULAR_FILE_MODES:
            found.add(path)
    return found


def _read_doc(root: Path, path: str) -> str | None:
    target = root / path
    if not target.parent.resolve().is_relative_to(root):
        return None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        data = handle.read(MAX_DOC_BYTES)
    return data.decode("utf-8", errors="replace")


def _read_docs(root: Path, paths: Iterable[str]) -> dict[str, str]:
    texts = {}
    for path in paths:
        text = _read_doc(root, path)
        if text is not None:
            texts[path] = text
    return texts


def read_repo_docs(controller_repo: Path) -> DocSources:
    root = controller_repo.resolve()
    tracked = _regular_tracked(root)
    instructions = instruction_paths(tracked)
    texts = _read_docs(root, instructions[:MAX_CANDIDATES])
    linked = linked_paths(texts, tracked)
    texts |= _read_docs(root, linked[: max(MAX_CANDIDATES - len(texts), 0)])
    return DocSources((*instructions, *linked), texts)
