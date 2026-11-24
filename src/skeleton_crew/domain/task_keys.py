import re
from enum import StrEnum


class RepoCommand(StrEnum):
    SETUP = "setup"
    CHECK = "check"


class KeyKind(StrEnum):
    WORKER = "worker"
    REFINE = "refine"
    SELECTOR = "selector"


TRANSCRIPTS_DIR = "transcripts"
SESSION_SUFFIX = ".jsonl"
COMMAND_SUFFIX = ".log"
_COMMANDS = "|".join(re.escape(command) for command in RepoCommand)
_TRANSCRIPT_RE = re.compile(
    rf"[a-z]+-\d+{re.escape(SESSION_SUFFIX)}|({_COMMANDS})-\d+{re.escape(COMMAND_SUFFIX)}",
    re.ASCII,
)

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


def container_name(prefix: str, task_key: str, role: str, nonce: str) -> str:
    return f"{prefix}{task_key}-{role}-{nonce}"


def numbered_name(stem: str, number: int, suffix: str) -> str:
    return f"{stem}-{number}{suffix}"


def name_number(name: str, stem: str, suffix: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(stem)}-(\d+){re.escape(suffix)}", name, re.ASCII)
    return int(match[1]) if match else None


def is_transcript_name(name: str) -> bool:
    return _TRANSCRIPT_RE.fullmatch(name) is not None
