import re

_KEY_RE = re.compile(r"worker-\d+|refine-\d+|run-\d+-selector", re.ASCII)


def worker_key(task_id: int) -> str:
    return f"worker-{task_id}"


def refine_key(task_id: int) -> str:
    return f"refine-{task_id}"


def selector_key(run_id: int) -> str:
    return f"run-{run_id}-selector"


def is_task_key(key: str) -> bool:
    return _KEY_RE.fullmatch(key) is not None
