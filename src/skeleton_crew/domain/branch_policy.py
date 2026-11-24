import re
import unicodedata

from skeleton_crew.domain.errors import PolicyViolation

BRANCH_PREFIX = "skeleton/"
_SLUG_MAX = 40
_ISSUE_DIGITS = 10
_MAX_ISSUE = 10**_ISSUE_DIGITS - 1
_BRANCH_RE = re.compile(
    rf"{re.escape(BRANCH_PREFIX)}([1-9][0-9]{{0,{_ISSUE_DIGITS - 1}}})-([a-z0-9]+(?:-[a-z0-9]+)*)"
)


def _slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:_SLUG_MAX].rstrip("-") or "issue"


def check_issue_number(number: int, *, maximum: int | None = None) -> None:
    if number < 1 or (maximum is not None and number > maximum):
        raise PolicyViolation(f"invalid issue number: {number}")


def branch_name(issue_number: int, title: str) -> str:
    check_issue_number(issue_number, maximum=_MAX_ISSUE)
    return f"{BRANCH_PREFIX}{issue_number}-{_slug(title)}"


def _match(branch: str) -> re.Match[str] | None:
    match = _BRANCH_RE.fullmatch(branch)
    if match is None or len(match.group(2)) > _SLUG_MAX:
        return None
    return match


def issue_number_from_branch(branch: str) -> int | None:
    match = _match(branch)
    return int(match.group(1)) if match else None


def is_skeleton_branch(branch: str) -> bool:
    return _match(branch) is not None


def check_push_target(branch: str, base_branch: str) -> None:
    if not is_skeleton_branch(branch):
        raise PolicyViolation(f"push target not allowed: {branch!r}")
    if branch == base_branch:
        raise PolicyViolation("push target is the base branch")


def pushed_by_controller(head: str, last_pushed: str | None) -> bool:
    return last_pushed is not None and head == last_pushed


def continuation_problem(head: str, last_pushed: str | None, closed_pr: int | None) -> str | None:
    if not pushed_by_controller(head, last_pushed):
        return "the branch has commits Skeleton Crew did not push"
    if closed_pr is not None:
        return f"PR #{closed_pr} was closed without merging"
    return None
