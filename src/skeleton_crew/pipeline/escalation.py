import logging

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import escalation_change
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.pipeline.texts import escalation_comment

log = logging.getLogger(__name__)


class Escalation(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def escalate_issue(ctx: RunContext, issue_number: int, reason: str) -> str | None:
    gw = ctx.gateway
    await ctx.audit(
        AuditKind.TASK_ESCALATED,
        AuditOutcome.INFO,
        f"escalating #{issue_number}",
        {"issue": issue_number, "reason": reason[:200]},
    )
    try:
        await gw.comment(
            issue_number, escalation_comment(reason, ctx.settings.limits.max_comment_chars)
        )
        await gw.change_labels(escalation_change(issue_number, gw.labels))
    except PolicyViolation as exc:
        log.warning("escalation of #%d was blocked: %s", issue_number, exc)
        return f"escalation blocked: {exc}"
    return None
