from collections.abc import Awaitable, Callable, Mapping

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome, AuditSink
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import LabelChange
from skeleton_crew.domain.write_policy import short


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
        summary = short(f"{action}: {exc}")
        await audit(AuditEntry(AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED, summary, payload))
        raise
    if dry_run:
        await audit(AuditEntry(AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN, action, payload))
        return dry_result
    try:
        result = await perform()
    except Exception as exc:
        failed = {**payload, "error": type(exc).__name__}
        await audit(
            AuditEntry(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED, f"{action} failed", failed)
        )
        raise
    await audit(AuditEntry(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED, action, payload))
    return result
