from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

REVIEW_CHECK_NAME = "skeleton-crew/review"
GITHUB_ACTIONS_SLUG = "github-actions"
_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


class ReviewCheck(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    CHANGES_REQUESTED = "changes_requested"


class CiState(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    NONE = "none"


@dataclass(frozen=True)
class FailedJob:
    name: str
    job_id: int | None
    details_url: str


@dataclass(frozen=True)
class CiStatus:
    state: CiState
    failed_jobs: list[FailedJob]


@dataclass(frozen=True)
class BranchRule:
    type: str
    ruleset_id: int | None
    parameters: Mapping[str, object]


def _tail(text: str, chars: int) -> str:
    return text[len(text) - chars :] if chars > 0 else ""


def format_job_logs(logs: Sequence[tuple[int, str]], max_chars: int) -> str:
    if not logs or max_chars <= 0:
        return ""
    room = max_chars // len(logs) - 1
    sections = []
    for job_id, text in logs:
        header = f"== job {job_id} ==\n"
        sections.append((header + _tail(text, room - len(header)))[: max(room, 0)])
    return "\n".join(sections)


def _run_failed(run: Mapping[str, Any]) -> FailedJob:
    app = run.get("app") or {}
    is_actions = isinstance(app, Mapping) and app.get("slug") == GITHUB_ACTIONS_SLUG
    return FailedJob(
        name=str(run.get("name") or ""),
        job_id=int(run["id"]) if is_actions and run.get("id") is not None else None,
        details_url=str(run.get("details_url") or run.get("html_url") or ""),
    )


def required_checks(rules: Sequence[BranchRule]) -> frozenset[str]:
    names: set[str] = set()
    for rule in rules:
        checks = rule.parameters.get("required_status_checks")
        if rule.type == "required_status_checks" and isinstance(checks, list):
            names |= {
                c["context"]
                for c in checks
                if isinstance(c, Mapping) and isinstance(c.get("context"), str) and c["context"]
            }
    # The controller completes the review check only after CI, so waiting for it would never end.
    return frozenset(names - {REVIEW_CHECK_NAME})


def _workflow_failures(workflow_runs: Sequence[Mapping[str, Any]]) -> list[FailedJob]:
    return [
        FailedJob(str(w.get("name") or ""), None, str(w.get("html_url") or ""))
        for w in workflow_runs
        if w.get("status") == "completed" and w.get("conclusion") not in _GREEN_CONCLUSIONS
    ]


def evaluate_ci(
    check_runs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    workflow_runs: Sequence[Mapping[str, Any]] = (),
    required: Collection[str] = (),
) -> CiStatus:
    reported = bool(workflow_runs)
    # A workflow run completes only after all its jobs, including ones that start late.
    pending = any(w.get("status") != "completed" for w in workflow_runs)
    failed: list[FailedJob] = []
    for run in check_runs:
        if run.get("name") == REVIEW_CHECK_NAME:
            continue
        reported = True
        if run.get("status") != "completed":
            pending = True
        elif run.get("conclusion") not in _GREEN_CONCLUSIONS:
            failed.append(_run_failed(run))
    for status in statuses:
        reported = True
        state = status.get("state")
        if state == "pending":
            pending = True
        elif state != "success":
            failed.append(
                FailedJob(
                    str(status.get("context") or ""), None, str(status.get("target_url") or "")
                )
            )
    # Failed jobs already explain a failed workflow; this covers a workflow that failed to start.
    failed = failed or _workflow_failures(workflow_runs)
    seen = {r.get("name") for r in check_runs} | {s.get("context") for s in statuses}
    if not reported and not required:
        return CiStatus(CiState.NONE, [])
    if pending or not set(required) <= seen:
        return CiStatus(CiState.PENDING, failed)
    if failed:
        return CiStatus(CiState.FAILURE, failed)
    return CiStatus(CiState.SUCCESS, [])
