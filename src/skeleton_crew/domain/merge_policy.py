import re
from collections.abc import Collection, Iterable

from skeleton_crew.domain.diff_parse import AddedLine
from skeleton_crew.domain.errors import Violation

_MARKER_RE = re.compile(r"(<{7}|>{7})(?: |$)")


def conflict_markers(lines: Iterable[AddedLine]) -> list[Violation]:
    return [
        Violation(line.path, line.line, "unresolved conflict marker")
        for line in lines
        if _MARKER_RE.match(line.text)
    ]


def check_resolution(changed_paths: Iterable[str], conflicted: Collection[str]) -> list[Violation]:
    return [
        Violation(path, None, "changed outside the merge conflict")
        for path in sorted(set(changed_paths))
        if path not in conflicted
    ]
