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


@dataclass(frozen=True)
class _SourceState:
    reported: bool
    pending: bool
    failed: tuple[FailedJob, ...]
    seen: frozenset[object] = frozenset()


def _check_run_state(check_runs: Sequence[Mapping[str, Any]]) -> _SourceState:
    runs = [r for r in check_runs if r.get("name") != REVIEW_CHECK_NAME]
    return _SourceState(
        reported=bool(runs),
        pending=any(r.get("status") != "completed" for r in runs),
        failed=tuple(
            _run_failed(r)
            for r in runs
            if r.get("status") == "completed" and r.get("conclusion") not in _GREEN_CONCLUSIONS
        ),
        seen=frozenset(r.get("name") for r in check_runs),
    )


def _status_state(statuses: Sequence[Mapping[str, Any]]) -> _SourceState:
    return _SourceState(
        reported=bool(statuses),
        pending=any(s.get("state") == "pending" for s in statuses),
        failed=tuple(
            FailedJob(str(s.get("context") or ""), None, str(s.get("target_url") or ""))
            for s in statuses
            if s.get("state") not in ("pending", "success")
        ),
        seen=frozenset(s.get("context") for s in statuses),
    )


def _workflow_state(workflow_runs: Sequence[Mapping[str, Any]]) -> _SourceState:
    # A workflow run completes only after all its jobs, including ones that start late.
    return _SourceState(
        reported=bool(workflow_runs),
        pending=any(w.get("status") != "completed" for w in workflow_runs),
        failed=tuple(
            FailedJob(str(w.get("name") or ""), None, str(w.get("html_url") or ""))
            for w in workflow_runs
            if w.get("status") == "completed" and w.get("conclusion") not in _GREEN_CONCLUSIONS
        ),
    )


def evaluate_ci(
    check_runs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    workflow_runs: Sequence[Mapping[str, Any]] = (),
    required: Collection[str] = (),
) -> CiStatus:
    sources = (_check_run_state(check_runs), _status_state(statuses))
    workflows = _workflow_state(workflow_runs)
    reported = workflows.reported or any(s.reported for s in sources)
    pending = workflows.pending or any(s.pending for s in sources)
    # Failed jobs already explain a failed workflow; this covers a workflow that failed to start.
    failed = [job for s in sources for job in s.failed] or list(workflows.failed)
    seen = frozenset().union(*(s.seen for s in sources))
    if not reported and not required:
        return CiStatus(CiState.NONE, [])
    if pending or not set(required) <= seen:
        return CiStatus(CiState.PENDING, failed)
    if failed:
        return CiStatus(CiState.FAILURE, failed)
    return CiStatus(CiState.SUCCESS, [])
