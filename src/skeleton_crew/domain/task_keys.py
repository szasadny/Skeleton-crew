import re
from enum import StrEnum


class KeyKind(StrEnum):
    WORKER = "worker"
    REFINE = "refine"
    SELECTOR = "selector"


_FORMATS: dict[KeyKind, tuple[str, str]] = {
    KeyKind.WORKER: ("worker-", ""),
    KeyKind.REFINE: ("refine-", ""),
    KeyKind.SELECTOR: ("run-", "-selector"),
}
_KINDS = tuple(_FORMATS)
_KEY_RE = re.compile(
    "|".join(
        f"{re.escape(prefix)}(\\d+){re.escape(suffix)}" for prefix, suffix in _FORMATS.values()
    ),
    re.ASCII,
)


def _key(kind: KeyKind, number: int) -> str:
    prefix, suffix = _FORMATS[kind]
    return f"{prefix}{number}{suffix}"


def worker_key(task_id: int) -> str:
    return _key(KeyKind.WORKER, task_id)


def refine_key(task_id: int) -> str:
    return _key(KeyKind.REFINE, task_id)


def selector_key(run_id: int) -> str:
    return _key(KeyKind.SELECTOR, run_id)


def is_task_key(key: str) -> bool:
    return _KEY_RE.fullmatch(key) is not None


def parse_task_key(key: str) -> tuple[KeyKind, int] | None:
    match = _KEY_RE.fullmatch(key)
    if match is None or match.lastindex is None:
        return None
    return _KINDS[match.lastindex - 1], int(match[match.lastindex])
