import re
from enum import StrEnum

_KEY_RE = re.compile(r"worker-(\d+)|refine-(\d+)|run-(\d+)-selector", re.ASCII)


class KeyKind(StrEnum):
    WORKER = "worker"
    REFINE = "refine"
    SELECTOR = "selector"


_KINDS = (KeyKind.WORKER, KeyKind.REFINE, KeyKind.SELECTOR)


def worker_key(task_id: int) -> str:
    return f"worker-{task_id}"


def refine_key(task_id: int) -> str:
    return f"refine-{task_id}"


def selector_key(run_id: int) -> str:
    return f"run-{run_id}-selector"


def is_task_key(key: str) -> bool:
    return _KEY_RE.fullmatch(key) is not None


def parse_task_key(key: str) -> tuple[KeyKind, int] | None:
    match = _KEY_RE.fullmatch(key)
    if match is None or match.lastindex is None:
        return None
    return _KINDS[match.lastindex - 1], int(match[match.lastindex])
