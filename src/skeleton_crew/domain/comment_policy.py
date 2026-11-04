import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from skeleton_crew.domain.errors import PolicyViolation


@dataclass(frozen=True)
class AddedLine:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class CommentFlag:
    path: str
    start_line: int
    length: int


@dataclass(frozen=True)
class ContestedFlag:
    path: str
    start_line: int
    length: int
    reason: str | None


_HUNK_RE = re.compile(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
_BLOCK_DELIMITERS = (('"""', '"""'), ("'''", "'''"), ("/*", "*/"), ("<!--", "-->"))
_COMMENT_PREFIXES = ("#", "//", "--", ";", "*", "%")
_NOT_COMMENTS = ("#include", "#define", "#if", "#pragma", "#region")
_PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")


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


def _classify(line: AddedLine) -> tuple[bool, str | None]:
    text = line.text.strip()
    for opener, closer in _BLOCK_DELIMITERS:
        if text.startswith(opener):
            return True, None if closer in text[len(opener) :] else closer
    if line.line == 1 and text.startswith("#!"):
        return False, None
    if text.startswith(_NOT_COMMENTS):
        return False, None
    return text.startswith(_COMMENT_PREFIXES) or text.casefold().startswith("rem "), None


def _comment_mask(lines: Sequence[AddedLine]) -> list[bool]:
    mask: list[bool] = []
    closer: str | None = None
    for index, line in enumerate(lines):
        if index and not _adjacent(lines[index - 1], line):
            closer = None
        if closer is None:
            is_comment, closer = _classify(line)
        else:
            is_comment = True
            if closer in line.text:
                closer = None
        mask.append(is_comment)
    return mask


def _adjacent(first: AddedLine, second: AddedLine) -> bool:
    return first.path == second.path and second.line == first.line + 1


def flag_long_comment_blocks(lines: Sequence[AddedLine], max_lines: int) -> list[CommentFlag]:
    lines = [line for line in lines if not line.path.casefold().endswith(_PROSE_SUFFIXES)]
    mask = _comment_mask(lines)
    flags: list[CommentFlag] = []
    start = 0
    while start < len(lines):
        if not mask[start]:
            start += 1
            continue
        end = start
        while end + 1 < len(lines) and mask[end + 1] and _adjacent(lines[end], lines[end + 1]):
            end += 1
        if end - start + 1 > max_lines:
            flags.append(CommentFlag(lines[start].path, lines[start].line, end - start + 1))
        start = end + 1
    return flags


def contest_flags(
    flags: Sequence[CommentFlag], reasons: Mapping[tuple[str, int], str]
) -> tuple[list[CommentFlag], list[ContestedFlag]]:
    open_flags: list[CommentFlag] = []
    contested: list[ContestedFlag] = []
    for flag in flags:
        reason = reasons.get((flag.path, flag.start_line))
        if reason is None:
            open_flags.append(flag)
        else:
            contested.append(ContestedFlag(flag.path, flag.start_line, flag.length, reason))
    return open_flags, contested


def uncontested(flags: Sequence[CommentFlag]) -> list[ContestedFlag]:
    return [ContestedFlag(f.path, f.start_line, f.length, None) for f in flags]
