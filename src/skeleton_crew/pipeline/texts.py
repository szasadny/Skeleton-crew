from collections.abc import Sequence

from skeleton_crew.domain.diff_policy import FileChange
from skeleton_crew.domain.schemas import NewIssue, RefineResult, ReviewResult
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.text_policy import sanitize_agent_text

COMMIT_AUTHOR = "Skeleton Crew <skeleton-crew@users.noreply.github.com>"
PR_TITLE_PREFIX = "[skeleton] "
# Above the schema's 72 so that inserted zero-width spaces never trigger the multi-line suffix.
PR_TITLE_CHARS = 100
ISSUE_TITLE_CHARS = 200
ESCALATION_INTRO = (
    "Skeleton Crew stopped working on this issue. A human needs to decide how to go on."
)
INTERRUPTED_COMMENT = (
    "Skeleton Crew was interrupted while working on this issue. "
    "Any open skeleton PR for it may be incomplete."
)
CONTINUED_SUMMARY = (
    "Skeleton Crew continued from an existing branch for this issue "
    "and merged the base branch into it."
)
REFINE_HEADER = "## Refined spec\n\n"
REFINE_QUESTIONS_HEADER = "\n\n### Open questions\n\n"
REFINE_RESERVED_CHARS = len(REFINE_HEADER) + len(REFINE_QUESTIONS_HEADER)
BLOCKERS_HEADER = "\n\n### Blockers\n\n"


def _cap(text: str, max_chars: int, reserved: int) -> str:
    return sanitize_agent_text(text, max(max_chars - reserved, 100))


def escalation_comment(reason: str, max_chars: int) -> str:
    prefix = f"{ESCALATION_INTRO}\n\nReason: "
    return prefix + _cap(reason, max_chars, len(prefix))


def continued_comment(branch: str) -> str:
    return (
        f"Skeleton Crew found an existing branch `{branch}` for this issue and continued from it."
    )


def pr_title(agent_title: str) -> str:
    return PR_TITLE_PREFIX + sanitize_agent_text(agent_title, PR_TITLE_CHARS)


def tag_blockers(role: Role, blockers: Sequence[str]) -> list[str]:
    return [f"{role.value}: {blocker}" for blocker in blockers]


def _blockers_section(blockers: Sequence[str]) -> str:
    if not blockers:
        return ""
    return BLOCKERS_HEADER + "\n".join(f"- {blocker}" for blocker in dict.fromkeys(blockers))


def pr_body(issue_number: int, summary: str, blockers: Sequence[str], max_chars: int) -> str:
    prefix = f"Closes #{issue_number}\n\n"
    return prefix + _cap(summary + _blockers_section(blockers), max_chars, len(prefix))


def commit_message(issue_number: int, agent_title: str) -> str:
    return f"{sanitize_agent_text(agent_title, PR_TITLE_CHARS)}\n\nIssue #{issue_number}"


def merge_message(base_branch: str, branch: str) -> str:
    return f"Merge {base_branch} into {branch}"


def new_issue_text(issue: NewIssue, source_issue: int, max_chars: int) -> tuple[str, str]:
    suffix = f"\n\nFound while working on #{source_issue}."
    title = sanitize_agent_text(issue.title, ISSUE_TITLE_CHARS)
    return title, _cap(issue.body, max_chars, len(suffix)) + suffix


def refine_comment(result: RefineResult, max_chars: int) -> str:
    text = result.spec_markdown
    if result.open_questions:
        questions = "\n".join(f"- {q}" for q in result.open_questions)
        text += f"{REFINE_QUESTIONS_HEADER}{questions}"
    return REFINE_HEADER + _cap(text, max_chars, len(REFINE_HEADER))


def review_text(result: ReviewResult) -> str:
    lines = [result.summary]
    for finding in result.findings:
        where = f"{finding.path}:{finding.line}" if finding.line else finding.path or "general"
        lines.append(f"- [{finding.severity}] {where}: {finding.message}")
    for doc in result.missing_doc_updates:
        lines.append(f"- [docs] {doc.path}: {doc.reason}")
    return "\n".join(lines)


def review_check_summary(
    result: ReviewResult, worker_blockers: Sequence[str], max_chars: int
) -> str:
    blockers = tag_blockers(Role.WORKER, worker_blockers) + tag_blockers(
        Role.REVIEWER, result.blockers
    )
    return sanitize_agent_text(review_text(result) + _blockers_section(blockers), max_chars)


def diff_stats(changes: Sequence[FileChange]) -> str:
    lines = []
    for change in changes:
        counts = "binary" if change.added is None else f"+{change.added} -{change.removed}"
        lines.append(f"{change.status.value} {change.path} ({counts})")
    return "\n".join(lines)
