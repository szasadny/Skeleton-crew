import re
from collections.abc import Iterator
from dataclasses import dataclass

from skeleton_crew.domain.errors import PolicyViolation


@dataclass(frozen=True)
class AddedLine:
    path: str
    line: int
    text: str


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
            match = _HUNK_RE.fullmatch(line)
            if match is None:
                raise PolicyViolation(f"unparseable hunk header: {line!r}")
            old_left = int(match.group(1) or "1")
            number = int(match.group(2))
            new_left = int(match.group(3) or "1")
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
                    result.append(AddedLine(path, number, body[1:]))
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
        elif line[:1] in ("+", " ") or (line.startswith("-") and not line.startswith("--- ")):
            raise PolicyViolation(f"diff line outside a hunk: {line!r}")
    return result
