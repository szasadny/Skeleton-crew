import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

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
class AddedLine:
    path: str
    line: int
    text: str


NO_MODE = "000000"
SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"
REGULAR_FILE_MODES = frozenset({"100644", "100755"})
_KNOWN_MODES = frozenset({NO_MODE, *REGULAR_FILE_MODES, SYMLINK_MODE, SUBMODULE_MODE})
_RAW_RE = re.compile(r":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{7,64}) ([0-9a-f]{7,64}) ([AMDT])")
_COUNT_RE = re.compile(r"0|[1-9][0-9]{0,9}")
_HUNK_RE = re.compile(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(quoted: str) -> str:
    if len(quoted) < 2 or not quoted.endswith('"'):
        raise PolicyViolation(f"unparseable quoted path: {quoted!r}")
    body, out, i = quoted[1:-1], bytearray(), 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out += char.encode()
            i += 1
        elif body[i + 1 : i + 2] in _ESCAPES:
            out.append(_ESCAPES[body[i + 1]])
            i += 2
        elif re.fullmatch(r"[0-3][0-7]{2}", body[i + 1 : i + 4]):
            out.append(int(body[i + 1 : i + 4], 8))
            i += 4
        else:
            raise PolicyViolation(f"unparseable quoted path: {quoted!r}")
    try:
        return out.decode()
    except UnicodeDecodeError:
        raise PolicyViolation(f"path is not UTF-8: {quoted!r}") from None


def _new_path(header: str) -> str | None:
    name = header[4:]
    if name == "/dev/null":
        return None
    if name.endswith("\t"):
        # git appends a tab to names that contain a space; a real tab would be quoted.
        name = name[:-1]
    if name.startswith('"'):
        name = _unquote(name)
    if not name.startswith("b/") or len(name) == 2:
        raise PolicyViolation(f"unexpected diff header: {header!r}")
    return name[2:]


def _hunk_header(line: str) -> tuple[int, int, int]:
    match = _HUNK_RE.fullmatch(line)
    if match is None:
        raise PolicyViolation(f"unparseable hunk header: {line!r}")
    return int(match.group(1) or "1"), int(match.group(2)), int(match.group(3) or "1")


def _read_hunk(lines: Iterator[str], path: str | None, header: str) -> list[AddedLine]:
    old_left, number, new_left = _hunk_header(header)
    added: list[AddedLine] = []
    while old_left or new_left:
        body = next(lines, None)
        if body is None:
            raise PolicyViolation("diff ends inside a hunk")
        kind = body[:1]
        if kind == "\\":
            continue
        if kind == "+" and new_left:
            if path is None:
                raise PolicyViolation("added line without a target file")
            added.append(AddedLine(path, number, body[1:]))
            number += 1
            new_left -= 1
        elif kind == "-" and old_left:
            old_left -= 1
        elif kind == " " and old_left and new_left:
            number += 1
            old_left -= 1
            new_left -= 1
        else:
            raise PolicyViolation(f"unexpected hunk line: {body!r}")
    return added


def _outside_hunk(line: str) -> bool:
    return line[:1] in ("+", " ") or (line.startswith("-") and not line.startswith("--- "))


def added_lines(unified_diff: str) -> list[AddedLine]:
    result: list[AddedLine] = []
    lines: Iterator[str] = iter(unified_diff.split("\n"))
    path: str | None = None
    for line in lines:
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            path = _new_path(line)
        elif line.startswith("@@"):
            result += _read_hunk(lines, path, line)
        elif _outside_hunk(line):
            raise PolicyViolation(f"diff line outside a hunk: {line!r}")
    return result


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
