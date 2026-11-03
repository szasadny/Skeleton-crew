import re
from collections.abc import Awaitable, Callable, Mapping

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.branch_policy import check_push_target
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import LabelChange, check_new_issue_labels
from skeleton_crew.domain.settings import LabelNames

AuditSink = Callable[[AuditKind, AuditOutcome, str, Mapping[str, object] | None], Awaitable[None]]

AUDIT_TEXT_CHARS = 200
MAX_TITLE_CHARS = 256
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def short(text: str) -> str:
    return text[:AUDIT_TEXT_CHARS]


def check_issue_number(number: int) -> None:
    if number < 1:
        raise PolicyViolation(f"invalid issue number: {number}")


def check_title(title: str) -> None:
    if not title.strip() or len(title) > MAX_TITLE_CHARS or "\n" in title or "\r" in title:
        raise PolicyViolation(f"title not allowed: {short(title)!r}")


def check_sha(sha: str) -> None:
    if not _SHA_RE.fullmatch(sha):
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


def label_payload(change: LabelChange) -> dict[str, object]:
    return {
        "issue": change.issue_number,
        "add": sorted(short(n) for n in change.add),
        "remove": sorted(short(n) for n in change.remove),
    }


async def guarded_write[T](
    audit: AuditSink,
    *,
    dry_run: bool,
    action: str,
    payload: Mapping[str, object],
    check: Callable[[], None],
    perform: Callable[[], Awaitable[T]],
    dry_result: T,
) -> T:
    try:
        check()
    except PolicyViolation as exc:
        await audit(
            AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED, short(f"{action}: {exc}"), payload
        )
        raise
    if dry_run:
        await audit(AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN, action, payload)
        return dry_result
    try:
        result = await perform()
    except Exception as exc:
        failed = {**payload, "error": type(exc).__name__}
        await audit(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED, f"{action} failed", failed)
        raise
    await audit(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED, action, payload)
    return result
