import logging
from collections.abc import Callable

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import LabelChange, escalation_change
from skeleton_crew.domain.settings import LabelNames
from skeleton_crew.domain.write_policy import short
from skeleton_crew.github.gateway import Gateway
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.texts import escalation_comment

log = logging.getLogger(__name__)

type LabelSwap = Callable[[int, LabelNames], LabelChange]


async def comment_and_relabel(gw: Gateway, number: int, body: str, change: LabelSwap) -> str | None:
    try:
        await gw.comment(number, body)
        await gw.change_labels(change(number, gw.labels))
    except PolicyViolation as exc:
        return str(exc)
    return None


async def escalate_issue(ctx: RunContext, issue_number: int, reason: str) -> str | None:
    await ctx.audit(
        AuditEntry(
            AuditKind.TASK_ESCALATED,
            AuditOutcome.INFO,
            f"escalating #{issue_number}",
            {"issue": issue_number, "reason": short(reason)},
        )
    )
    body = escalation_comment(reason, ctx.settings.limits.max_comment_chars)
    blocked = await comment_and_relabel(ctx.gateway, issue_number, body, escalation_change)
    if blocked is None:
        return None
    log.warning("escalation of #%d was blocked: %s", issue_number, blocked)
    return f"escalation blocked: {blocked}"
