import unicodedata
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath

from skeleton_crew.domain.diff_parse import SUBMODULE_MODE, SYMLINK_MODE, FileChange
from skeleton_crew.domain.errors import Violation

_WHOLE_DIFF = "*"


def _is_git_dir_segment(segment: str) -> bool:
    # Windows/macOS checkouts resolve ".git.", ".git " and the 8.3 name "git~1" to ".git".
    folded = segment.rstrip(" .").casefold()
    return folded == ".git" or folded.startswith(".git:") or folded == "git~1"


_PATH_RULES: tuple[tuple[Callable[[str], bool], str], ...] = (
    (lambda path: not path, "empty path"),
    (lambda path: path.startswith("/"), "absolute path"),
    (lambda path: "\\" in path, "backslash in path"),
    (
        lambda path: any(unicodedata.category(c) in ("Cc", "Cf") for c in path),
        "control character in path",
    ),
    (lambda path: any(s in ("", ".") for s in path.split("/")), "empty or '.' path segment"),
    (lambda path: ".." in path.split("/"), "'..' in path"),
    (lambda path: any(_is_git_dir_segment(s) for s in path.split("/")), "'.git' path segment"),
)


def _path_problem(path: str) -> str | None:
    return next((reason for applies, reason in _PATH_RULES if applies(path)), None)


def _is_protected(path: str, patterns: Sequence[str]) -> bool:
    pure = PurePosixPath(path)
    for pattern in patterns:
        if pure.full_match(pattern, case_sensitive=False):
            return True
        if pattern.endswith("/**") and pure.full_match(pattern[:-3], case_sensitive=False):
            return True
    return False


def _change_violations(change: FileChange, protected_paths: Sequence[str]) -> list[Violation]:
    problem = _path_problem(change.path)
    if problem is not None:
        return [Violation(change.path, None, problem)]
    violations = []
    if _is_protected(change.path, protected_paths):
        violations.append(Violation(change.path, None, "protected path"))
    if change.new_mode == SYMLINK_MODE:
        violations.append(Violation(change.path, None, "symlink not allowed"))
    elif change.new_mode == SUBMODULE_MODE:
        violations.append(Violation(change.path, None, "submodule not allowed"))
    return violations


def _size_violation(changes: Sequence[FileChange], max_lines: int) -> Violation | None:
    total = sum(c.added + (c.removed or 0) for c in changes if c.added is not None)
    if total <= max_lines:
        return None
    return Violation(_WHOLE_DIFF, None, f"diff has {total} lines, limit is {max_lines}")


def check_diff(
    changes: Sequence[FileChange], protected_paths: Sequence[str], max_lines: int
) -> list[Violation]:
    if not changes:
        return [Violation(_WHOLE_DIFF, None, "no changes")]
    violations = [v for change in changes for v in _change_violations(change, protected_paths)]
    size = _size_violation(changes, max_lines)
    return violations if size is None else [*violations, size]
