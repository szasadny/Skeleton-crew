from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.domain.contest_policy import CheckId, CheckInput, Finding
from skeleton_crew.domain.diff_lines import AddedLine
from skeleton_crew.domain.settings import AppSettings


@dataclass(frozen=True)
class CommentFlag:
    path: str
    start_line: int
    length: int


_BLOCK_DELIMITERS = (('"""', '"""'), ("'''", "'''"), ("/*", "*/"), ("<!--", "-->"))
_COMMENT_PREFIXES = ("#", "//", "--", ";", "*", "%")
_NOT_COMMENTS = ("#include", "#define", "#if", "#pragma", "#region")
_PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")


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


def comment_block_findings(diff: CheckInput) -> list[Finding]:
    limit = diff.settings.limits.max_code_comment_lines
    return [
        Finding(
            CheckId.COMMENT_BLOCK,
            flag.path,
            flag.start_line,
            f"comment block of {flag.length} lines, limit is {limit}",
        )
        for flag in flag_long_comment_blocks(diff.lines, limit)
    ]


def describe_comment_blocks(settings: AppSettings) -> str:
    return f"comment blocks longer than {settings.limits.max_code_comment_lines} lines"
