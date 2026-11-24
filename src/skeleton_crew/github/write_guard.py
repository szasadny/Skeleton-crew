from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome, AuditSink
from skeleton_crew.domain.branch_policy import check_issue_number
from skeleton_crew.domain.ci_policy import ReviewCheck
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import LabelChange, check_label_change
from skeleton_crew.domain.settings import LabelNames
from skeleton_crew.domain.write_policy import (
    check_new_issue,
    check_open_pr,
    check_push,
    check_sha,
    short,
)
from skeleton_crew.domain.write_policy import new_issue_labels as new_issue_labels


class WriteAction(StrEnum):
    CHANGE_LABELS = "change_labels"
    COMMENT = "comment"
    CREATE_ISSUE = "create_issue"
    OPEN_PR = "open_pr"
    SET_REVIEW_CHECK = "set_review_check"
    PUSH_BRANCH = "push_branch"


@dataclass(frozen=True)
class WriteSpec:
    action: WriteAction
    payload: Mapping[str, object]
    check: Callable[[], None]


def label_payload(change: LabelChange) -> dict[str, object]:
    return {
        "issue": change.issue_number,
        "add": sorted(short(n) for n in change.add),
        "remove": sorted(short(n) for n in change.remove),
    }


def label_spec(change: LabelChange, labels: LabelNames) -> WriteSpec:
    return WriteSpec(
        WriteAction.CHANGE_LABELS,
        label_payload(change),
        lambda: check_label_change(change, labels),
    )


def comment_spec(issue_number: int, body: str) -> WriteSpec:
    return WriteSpec(
        WriteAction.COMMENT,
        {"issue": issue_number, "body": short(body)},
        lambda: check_issue_number(issue_number),
    )


def new_issue_spec(title: str, body: str, labels: LabelNames) -> WriteSpec:
    def check() -> None:
        check_new_issue(title, labels)

    payload = {"title": short(title), "body": short(body), "labels": new_issue_labels(labels)}
    return WriteSpec(WriteAction.CREATE_ISSUE, payload, check)


def open_pr_spec(branch: str, title: str, body: str, base_branch: str) -> WriteSpec:
    return WriteSpec(
        WriteAction.OPEN_PR,
        {"branch": short(branch), "title": short(title), "body": short(body)},
        lambda: check_open_pr(branch, title, base_branch),
    )


def review_check_spec(sha: str, state: ReviewCheck, summary: str) -> WriteSpec:
    return WriteSpec(
        WriteAction.SET_REVIEW_CHECK,
        {"sha": short(sha), "state": state.value, "summary": short(summary)},
        lambda: check_sha(sha),
    )


def push_spec(branch: str, sha: str, base_branch: str, expected_sha: str | None) -> WriteSpec:
    def check() -> None:
        check_push(branch, sha, base_branch)
        if expected_sha is not None:
            check_sha(expected_sha)

    payload: dict[str, object] = {"branch": short(branch), "sha": short(sha)}
    if expected_sha is not None:
        payload["expected"] = expected_sha
    return WriteSpec(WriteAction.PUSH_BRANCH, payload, check)


class WriteGuard:
    def __init__(self, audit: AuditSink, *, dry_run: bool) -> None:
        self._audit = audit
        self._dry_run = dry_run

    async def run[T](self, spec: WriteSpec, perform: Callable[[], Awaitable[T]]) -> T | None:
        action, payload = spec.action.value, spec.payload
        try:
            spec.check()
        except PolicyViolation as exc:
            summary = short(f"{action}: {exc}")
            entry = AuditEntry(AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED, summary, payload)
            await self._audit(entry)
            raise
        if self._dry_run:
            await self._audit(
                AuditEntry(AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN, action, payload)
            )
            return None
        try:
            result = await perform()
        except Exception as exc:
            failed = {**payload, "error": type(exc).__name__}
            await self._audit(
                AuditEntry(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED, f"{action} failed", failed)
            )
            raise
        await self._audit(AuditEntry(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED, action, payload))
        return result
