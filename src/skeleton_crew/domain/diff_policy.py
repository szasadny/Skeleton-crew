import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from skeleton_crew.domain.errors import PolicyViolation


class ChangeStatus(StrEnum):
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    TYPE_CHANGED = "T"


@dataclass(frozen=True)
class FileChange:
    path: str
    status: ChangeStatus
    old_mode: str
    new_mode: str
    added: int | None
    removed: int | None


@dataclass(frozen=True)
class DiffViolation:
    path: str
    reason: str


NO_MODE = "000000"
SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"
_KNOWN_MODES = frozenset({NO_MODE, "100644", "100755", SYMLINK_MODE, SUBMODULE_MODE})
_RAW_RE = re.compile(r":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{7,64}) ([0-9a-f]{7,64}) ([AMDT])")
_COUNT_RE = re.compile(r"0|[1-9][0-9]{0,9}")


def _records(raw: str, label: str) -> list[str]:
    if not raw:
        return []
    if not raw.endswith("\0"):
        raise PolicyViolation(f"{label} output not NUL-terminated")
    return raw[:-1].split("\0")


def _parse_raw(raw_z: str) -> list[tuple[str, ChangeStatus, str, str]]:
    tokens = _records(raw_z, "raw")
    if len(tokens) % 2:
        raise PolicyViolation("raw output has a dangling record")
    entries = []
    for header, path in zip(tokens[::2], tokens[1::2], strict=True):
        match = _RAW_RE.fullmatch(header)
        if match is None or not path:
            raise PolicyViolation(f"unparseable raw diff record: {header!r}")
        old_mode, new_mode, _, _, status_code = match.groups()
        status = ChangeStatus(status_code)
        if old_mode not in _KNOWN_MODES or new_mode not in _KNOWN_MODES:
            raise PolicyViolation(f"unknown file mode for {path!r}")
        if (old_mode == NO_MODE) != (status is ChangeStatus.ADDED) or (new_mode == NO_MODE) != (
            status is ChangeStatus.DELETED
        ):
            raise PolicyViolation(f"modes inconsistent with status for {path!r}")
        entries.append((path, status, old_mode, new_mode))
    return entries


def _parse_count(value: str) -> int:
    if not _COUNT_RE.fullmatch(value):
        raise PolicyViolation(f"unparseable numstat count: {value!r}")
    return int(value)


def _parse_numstat(numstat_z: str) -> list[tuple[str, int | None, int | None]]:
    entries: list[tuple[str, int | None, int | None]] = []
    for record in _records(numstat_z, "numstat"):
        parts = record.split("\t", 2)
        if len(parts) != 3 or not parts[2]:
            raise PolicyViolation(f"unparseable numstat record: {record!r}")
        added, removed, path = parts
        if added == "-" and removed == "-":
            entries.append((path, None, None))
        else:
            entries.append((path, _parse_count(added), _parse_count(removed)))
    return entries


def parse_diff_summary(raw_z: str, numstat_z: str) -> list[FileChange]:
    raw = _parse_raw(raw_z)
    numstat = _parse_numstat(numstat_z)
    if len(raw) != len(numstat):
        raise PolicyViolation("raw and numstat outputs disagree")
    if len({path for path, *_ in raw}) != len(raw):
        raise PolicyViolation("duplicate path in diff")
    changes = []
    for (path, status, old_mode, new_mode), (num_path, added, removed) in zip(
        raw, numstat, strict=True
    ):
        if path != num_path:
            raise PolicyViolation(f"raw and numstat paths disagree: {path!r} != {num_path!r}")
        changes.append(FileChange(path, status, old_mode, new_mode, added, removed))
    return changes


def _is_git_dir_segment(segment: str) -> bool:
    # Windows/macOS checkouts resolve ".git.", ".git " and the 8.3 name "git~1" to ".git".
    folded = segment.rstrip(" .").casefold()
    return folded == ".git" or folded.startswith(".git:") or folded == "git~1"


def _path_problem(path: str) -> str | None:
    if not path:
        return "empty path"
    if path.startswith("/"):
        return "absolute path"
    if "\\" in path:
        return "backslash in path"
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in path):
        return "control character in path"
    segments = path.split("/")
    if any(s in ("", ".") for s in segments):
        return "empty or '.' path segment"
    if ".." in segments:
        return "'..' in path"
    if any(_is_git_dir_segment(s) for s in segments):
        return "'.git' path segment"
    return None


def _is_protected(path: str, patterns: Sequence[str]) -> bool:
    pure = PurePosixPath(path)
    for pattern in patterns:
        if pure.full_match(pattern, case_sensitive=False):
            return True
        if pattern.endswith("/**") and pure.full_match(pattern[:-3], case_sensitive=False):
            return True
    return False


def check_diff(
    changes: Sequence[FileChange], protected_paths: Sequence[str], max_lines: int
) -> list[DiffViolation]:
    if not changes:
        return [DiffViolation("*", "no changes")]
    violations = []
    for change in changes:
        problem = _path_problem(change.path)
        if problem is not None:
            violations.append(DiffViolation(change.path, problem))
            continue
        if _is_protected(change.path, protected_paths):
            violations.append(DiffViolation(change.path, "protected path"))
        if change.new_mode == SYMLINK_MODE:
            violations.append(DiffViolation(change.path, "symlink not allowed"))
        elif change.new_mode == SUBMODULE_MODE:
            violations.append(DiffViolation(change.path, "submodule not allowed"))
    total = sum(c.added + (c.removed or 0) for c in changes if c.added is not None)
    if total > max_lines:
        violations.append(DiffViolation("*", f"diff has {total} lines, limit is {max_lines}"))
    return violations
