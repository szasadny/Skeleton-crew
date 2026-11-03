import re
from pathlib import Path

from skeleton_crew.core.logging import redact

MAX_TRANSCRIPT_BYTES = 500 * 1024
_NAME_RE = re.compile(r"^[a-z]+-\d+\.jsonl$")
_KEY_RE = re.compile(r"^(worker-\d+|refine-\d+|run-\d+-selector)$")


def _transcripts_dir(tasks_dir: Path, key: str) -> Path | None:
    if not _KEY_RE.fullmatch(key):
        return None
    directory = tasks_dir / key / "transcripts"
    return directory if directory.is_dir() and not directory.is_symlink() else None


def list_transcripts(tasks_dir: Path, key: str) -> list[str]:
    directory = _transcripts_dir(tasks_dir, key)
    if directory is None:
        return []
    return sorted(
        path.name
        for path in directory.iterdir()
        if _NAME_RE.fullmatch(path.name) and path.is_file() and not path.is_symlink()
    )


def read_transcript(
    tasks_dir: Path, key: str, name: str, secrets: list[str]
) -> tuple[str, bool] | None:
    directory = _transcripts_dir(tasks_dir, key)
    if directory is None or not _NAME_RE.fullmatch(name):
        return None
    path = directory / name
    if path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        truncated = size > MAX_TRANSCRIPT_BYTES
        handle.seek(max(0, size - MAX_TRANSCRIPT_BYTES))
        data = handle.read(MAX_TRANSCRIPT_BYTES)
    if truncated:
        # Drop the cut-off first line so a partial secret cannot slip past redaction.
        data = data.partition(b"\n")[2]
    return redact(data.decode("utf-8", errors="replace"), secrets), truncated
