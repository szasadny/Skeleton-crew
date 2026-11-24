from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from skeleton_crew.domain.comment_policy import flag_long_comment_blocks
from skeleton_crew.domain.diff_parse import AddedLine
from skeleton_crew.domain.errors import Violation
from skeleton_crew.domain.settings import AppSettings


class CheckId(StrEnum):
    COMMENT_BLOCK = "comment_block"


@dataclass(frozen=True)
class Finding:
    check: CheckId
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class Contest:
    finding: Finding
    reason: str | None


@dataclass(frozen=True)
class CheckInput:
    lines: Sequence[AddedLine]
    settings: AppSettings


@dataclass(frozen=True)
class ContestableCheck:
    id: CheckId
    run: Callable[[CheckInput], list[Finding]]
    describe: Callable[[AppSettings], str]


class _Located(Protocol):
    @property
    def check(self) -> CheckId: ...
    @property
    def path(self) -> str: ...
    @property
    def line(self) -> int: ...


type ObjectionKey = tuple[CheckId, str, int]


def objection_key(located: _Located) -> ObjectionKey:
    return located.check, located.path, located.line


def contest(
    findings: Sequence[Finding], reasons: Mapping[ObjectionKey, str]
) -> tuple[list[Finding], list[Contest]]:
    open_findings: list[Finding] = []
    contested: list[Contest] = []
    for finding in findings:
        reason = reasons.get(objection_key(finding))
        if reason is None:
            open_findings.append(finding)
        else:
            contested.append(Contest(finding, reason))
    return open_findings, contested


def uncontested(findings: Sequence[Finding]) -> list[Contest]:
    return [Contest(f, None) for f in findings]


def finding_violation(finding: Finding) -> Violation:
    reason = f"{finding.message} (contestable as `{finding.check}`)"
    return Violation(finding.path, finding.line, reason)


def comment_block_findings(diff: CheckInput) -> list[Finding]:
    limit = diff.settings.limits.max_code_comment_lines
    return [
        Finding(
            CheckId.COMMENT_BLOCK,
            flag.path,
            flag.start_line,
            f"comment block of {flag.length} lines, limit is {limit}",
        )
        for flag in flag_long_comment_blocks(diff.lines, limit)
    ]


def describe_comment_blocks(settings: AppSettings) -> str:
    return f"comment blocks longer than {settings.limits.max_code_comment_lines} lines"


# Hard guardrails (diff_policy, secrets, merge_policy) must never be registered here.
CHECKS: tuple[ContestableCheck, ...] = (
    ContestableCheck(CheckId.COMMENT_BLOCK, comment_block_findings, describe_comment_blocks),
)


def run_checks(diff: CheckInput) -> list[Finding]:
    return [finding for check in CHECKS for finding in check.run(diff)]


def describe_checks(settings: AppSettings) -> list[tuple[CheckId, str]]:
    return [(check.id, check.describe(settings)) for check in CHECKS]
