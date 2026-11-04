from collections.abc import Callable
from dataclasses import dataclass

from skeleton_crew.domain.comment_policy import comment_block_findings, describe_comment_blocks
from skeleton_crew.domain.contest_policy import CheckId, CheckInput, Finding
from skeleton_crew.domain.settings import AppSettings


@dataclass(frozen=True)
class ContestableCheck:
    id: CheckId
    run: Callable[[CheckInput], list[Finding]]
    describe: Callable[[AppSettings], str]


# Hard guardrails (diff_policy, secrets, merge_policy) must never be registered here.
CHECKS: tuple[ContestableCheck, ...] = (
    ContestableCheck(CheckId.COMMENT_BLOCK, comment_block_findings, describe_comment_blocks),
)


def run_checks(diff: CheckInput) -> list[Finding]:
    return [finding for check in CHECKS for finding in check.run(diff)]


def describe_checks(settings: AppSettings) -> list[tuple[CheckId, str]]:
    return [(check.id, check.describe(settings)) for check in CHECKS]
