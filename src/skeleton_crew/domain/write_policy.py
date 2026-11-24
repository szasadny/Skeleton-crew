from skeleton_crew.domain.branch_policy import check_push_target
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.git_refs import is_full_sha
from skeleton_crew.domain.label_policy import check_new_issue_labels
from skeleton_crew.domain.settings import LabelNames

MAX_TITLE_CHARS = 256
AUDIT_TEXT_CHARS = 200


def short(text: str) -> str:
    return text[:AUDIT_TEXT_CHARS]


def check_title(title: str) -> None:
    if not title.strip() or len(title) > MAX_TITLE_CHARS or "\n" in title or "\r" in title:
        raise PolicyViolation(f"title not allowed: {short(title)!r}")


def check_sha(sha: str) -> None:
    if not is_full_sha(sha):
        raise PolicyViolation(f"not a full commit sha: {short(sha)!r}")


def check_new_issue(title: str, labels: LabelNames) -> frozenset[str]:
    check_title(title)
    to_set = frozenset({labels.hitl})
    check_new_issue_labels(to_set, labels)
    return to_set


def check_open_pr(branch: str, title: str, base_branch: str) -> None:
    check_push_target(branch, base_branch)
    check_title(title)


def check_push(branch: str, sha: str, base_branch: str) -> None:
    check_push_target(branch, base_branch)
    check_sha(sha)
