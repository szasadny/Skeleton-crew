import pytest

from skeleton_crew.domain.ci_policy import (
    REVIEW_CHECK_NAME,
    BranchRule,
    CiState,
    FailedJob,
    evaluate_ci,
    format_job_logs,
    required_checks,
)

Raw = dict[str, object]


def _run(name: str, status: str, conclusion: str | None, slug: str = "github-actions") -> Raw:
    return {
        "id": 42,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": slug},
        "details_url": "https://ci/42",
    }


def _workflow(name: str, status: str, conclusion: str | None) -> Raw:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": "https://wf"}


@pytest.mark.parametrize(
    ("runs", "statuses", "expected"),
    [
        ([], [], CiState.NONE),
        ([_run(REVIEW_CHECK_NAME, "completed", "failure")], [], CiState.NONE),
        ([_run(REVIEW_CHECK_NAME, "in_progress", None)], [], CiState.NONE),
        (
            [_run(REVIEW_CHECK_NAME, "in_progress", None), _run("t", "completed", "success")],
            [],
            CiState.SUCCESS,
        ),
        (
            [_run("t", "completed", "success"), _run("l", "completed", "skipped")],
            [],
            CiState.SUCCESS,
        ),
        ([_run("t", "completed", "neutral")], [{"state": "success"}], CiState.SUCCESS),
        ([_run("t", "queued", None)], [], CiState.PENDING),
        ([_run("t", "in_progress", None), _run("u", "completed", "failure")], [], CiState.PENDING),
        ([_run("t", "completed", "success")], [{"state": "pending"}], CiState.PENDING),
        ([_run("t", "completed", "timed_out")], [], CiState.FAILURE),
        ([_run("t", "completed", "cancelled")], [], CiState.FAILURE),
        ([_run("t", "completed", "action_required")], [], CiState.FAILURE),
        ([_run("t", "completed", "startup_failure")], [], CiState.FAILURE),
        ([_run("t", "completed", "stale")], [], CiState.FAILURE),
        ([], [{"state": "error", "context": "ext"}], CiState.FAILURE),
        (
            [_run(REVIEW_CHECK_NAME, "completed", "success")],
            [{"state": "failure"}],
            CiState.FAILURE,
        ),
    ],
)
def test_evaluate_ci(runs: list[Raw], statuses: list[Raw], expected: CiState) -> None:
    assert evaluate_ci(runs, statuses).state == expected


GREEN_LINT = [_run("lint", "completed", "success")]


@pytest.mark.parametrize(
    ("workflows", "required", "expected"),
    [
        ([], set(), CiState.SUCCESS),
        ([_workflow("CI", "in_progress", None)], set(), CiState.PENDING),
        ([_workflow("CI", "queued", None)], set(), CiState.PENDING),
        ([_workflow("CI", "completed", "success")], set(), CiState.SUCCESS),
        ([_workflow("CI", "completed", "startup_failure")], set(), CiState.FAILURE),
        ([], {"lint"}, CiState.SUCCESS),
        ([], {"lint", "tests"}, CiState.PENDING),
        ([], {"ext"}, CiState.PENDING),
    ],
)
def test_evaluate_ci_waits_for_workflows_and_required_checks(
    workflows: list[Raw], required: set[str], expected: CiState
) -> None:
    assert evaluate_ci(GREEN_LINT, [], workflows, required).state == expected


def test_required_status_counts_as_reported() -> None:
    status = evaluate_ci([], [{"state": "success", "context": "ext"}], [], {"ext"})
    assert status.state == CiState.SUCCESS


def test_missing_required_check_is_pending_even_when_nothing_reported() -> None:
    assert evaluate_ci([], [], [], {"tests"}).state == CiState.PENDING


def test_failed_workflow_is_reported_only_without_failed_jobs() -> None:
    failed_wf = [_workflow("CI", "completed", "failure")]
    alone = evaluate_ci([], [], failed_wf)
    with_job = evaluate_ci([_run("a", "completed", "failure")], [], failed_wf)

    assert alone.failed_jobs == [FailedJob("CI", None, "https://wf")]
    assert [j.name for j in with_job.failed_jobs] == ["a"]


def test_required_checks_reads_ruleset_contexts_and_skips_the_review_check() -> None:
    rules = [
        BranchRule("pull_request", 1, {"required_approving_review_count": 1}),
        BranchRule(
            "required_status_checks",
            2,
            {
                "required_status_checks": [
                    {"context": "tests", "integration_id": 15368},
                    {"context": REVIEW_CHECK_NAME},
                    {"context": ""},
                    {"context": 7},
                    "lint",
                ]
            },
        ),
        BranchRule("required_status_checks", 3, {"required_status_checks": "lint"}),
        BranchRule("required_status_checks", 4, {}),
    ]
    assert required_checks(rules) == {"tests"}


def test_failed_jobs_carry_job_id_only_for_actions() -> None:
    status = evaluate_ci(
        [_run("a", "completed", "failure"), _run("b", "completed", "failure", slug="circleci")],
        [{"state": "failure", "context": "ext", "target_url": "https://ext"}],
    )
    assert status.failed_jobs == [
        FailedJob("a", 42, "https://ci/42"),
        FailedJob("b", None, "https://ci/42"),
        FailedJob("ext", None, "https://ext"),
    ]


def test_format_job_logs_respects_budget() -> None:
    logs = [(1, "a" * 100), (2, "b" * 100), (3, "c" * 100)]
    for max_chars in (0, 5, 30, 100, 1000):
        assert len(format_job_logs(logs, max_chars)) <= max_chars
    assert format_job_logs(logs, 1000).count("== job") == 3
