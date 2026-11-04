from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from skeleton_crew.domain.diff_lines import AddedLine
from skeleton_crew.domain.diff_policy import FileChange
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
    changes: Sequence[FileChange]
    lines: Sequence[AddedLine]
    settings: AppSettings


type ObjectionKey = tuple[CheckId, str, int]


def objection_key(finding: Finding) -> ObjectionKey:
    return finding.check, finding.path, finding.line


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


def violation_text(finding: Finding) -> str:
    return f"{finding.path}:{finding.line}: {finding.message} (contestable as `{finding.check}`)"
