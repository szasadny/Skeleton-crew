import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from github_helpers import API, SHA, AuditLog, make_gateway

from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.github.gateway import (
    LOGS_UNAVAILABLE,
    REVIEW_CHECK_NAME,
    CiState,
    FailedJob,
    evaluate_ci,
    format_job_logs,
)

ISSUES = f"{API}/repos/o/r/issues"
GREEN, RED = CiState.SUCCESS, CiState.FAILURE
Raw = dict[str, object]


def _issue(number: int, **extra: object) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": None,
        "labels": [{"name": "skeleton"}],
        "user": {"login": "alice"},
        "html_url": f"https://github.com/o/r/issues/{number}",
        **extra,
    }


def _event(kind: str, login: str, at: str) -> dict[str, object]:
    return {"event": kind, "actor": {"login": login}, "created_at": at}


def _paged(pages: list[list[dict[str, object]]], url: str) -> respx.Route:
    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < len(pages):
            headers["link"] = f'<{url}?page={page + 1}>; rel="next"'
        return httpx.Response(200, json=pages[page - 1], headers=headers)

    return respx.get(url).mock(side_effect=respond)


@respx.mock
async def test_list_open_issues_paginates_and_skips_pull_requests() -> None:
    route = _paged([[_issue(1), _issue(2, pull_request={"url": "x"})], [_issue(3)]], ISSUES)

    issues = await make_gateway(AuditLog()).list_open_issues("skeleton")

    assert [i.number for i in issues] == [1, 3]
    assert issues[0].body == ""
    assert issues[0].labels == frozenset({"skeleton"})
    assert issues[0].author == "alice"
    first = route.calls[0].request.url.params
    assert first["labels"] == "skeleton"
    assert first["state"] == "open"


@respx.mock
async def test_trust_data_collects_labels_renames_body_edits_and_comments() -> None:
    _paged(
        [
            [
                {
                    "event": "labeled",
                    "actor": {"login": "maint"},
                    "label": {"name": "skeleton"},
                    "created_at": "2026-01-02T00:00:00Z",
                },
                {
                    "event": "renamed",
                    "actor": {"login": "eve"},
                    "created_at": "2026-01-03T00:00:00Z",
                },
            ],
            [
                {
                    "event": "unlabeled",
                    "actor": None,
                    "label": {"name": "skeleton"},
                    "created_at": "2026-01-04T00:00:00Z",
                },
                {
                    "event": "commented",
                    "actor": {"login": "x"},
                    "created_at": "2026-01-05T00:00:00Z",
                },
            ],
        ],
        f"{ISSUES}/7/timeline",
    )
    _paged(
        [[{"user": {"login": "bob"}, "body": "hi", "created_at": "2026-01-06T00:00:00Z"}]],
        f"{ISSUES}/7/comments",
    )
    pages = [
        {"nodes": [{"editedAt": "2026-01-07T00:00:00Z", "editor": {"login": "eve"}}], "next": True},
        {"nodes": [{"editedAt": "2026-01-01T00:00:00Z", "editor": None}], "next": False},
    ]
    cursors: list[object] = []

    def graphql(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        cursors.append(variables["cursor"])
        page = pages[len(cursors) - 1]
        edits = {
            "nodes": page["nodes"],
            "pageInfo": {"hasNextPage": page["next"], "endCursor": "c1"},
        }
        data = {"repository": {"issue": {"userContentEdits": edits}}}
        return httpx.Response(200, json={"data": data})

    respx.post(f"{API}/graphql").mock(side_effect=graphql)

    data = await make_gateway(AuditLog()).trust_data(7)

    assert [(e.actor, e.added) for e in data.label_events] == [("maint", True), ("", False)]
    assert data.label_events[0].at == datetime(2026, 1, 2, tzinfo=UTC)
    assert [e.editor for e in data.edits] == ["eve", "eve", ""]
    assert cursors == [None, "c1"]
    assert [(c.author, c.body) for c in data.comments] == [("bob", "hi")]


@respx.mock
async def test_permission_of_returns_role_name_and_caches() -> None:
    route = respx.get(f"{API}/repos/o/r/collaborators/Bob/permission").mock(
        return_value=httpx.Response(200, json={"permission": "write", "role_name": "maintain"})
    )
    gateway = make_gateway(AuditLog())

    assert await gateway.permission_of("Bob") == "maintain"
    assert await gateway.permission_of("Bob") == "maintain"
    assert route.call_count == 1


def _pull(number: int, ref: str, *, head_repo: int | None = 1) -> dict[str, object]:
    repo = None if head_repo is None else {"id": head_repo}
    return {
        "number": number,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": ref, "sha": SHA, "repo": repo},
        "base": {"ref": "main", "repo": {"id": 1}},
    }


@respx.mock
async def test_open_skeleton_pr_for_ignores_forks_and_other_issues() -> None:
    _paged(
        [
            [_pull(10, "skeleton/5-fix", head_repo=2), _pull(11, "skeleton/6-other")],
            [_pull(12, "skeleton/5-fix", head_repo=None), _pull(13, "skeleton/5-fix")],
        ],
        f"{API}/repos/o/r/pulls",
    )
    gateway = make_gateway(AuditLog())

    pr = await gateway.open_skeleton_pr_for(5)

    assert pr is not None
    assert pr.number == 13
    assert pr.head_sha == SHA
    assert await gateway.open_skeleton_pr_for(9) is None


def _run(name: str, status: str, conclusion: str | None, slug: str = "github-actions") -> Raw:
    return {
        "id": 42,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": slug},
        "details_url": "https://ci/42",
    }


@pytest.mark.parametrize(
    ("runs", "statuses", "expected"),
    [
        ([], [], CiState.NONE),
        ([_run(REVIEW_CHECK_NAME, "completed", "failure")], [], CiState.NONE),
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


@respx.mock
async def test_ci_status_paginates_check_runs_and_statuses() -> None:
    runs_url = f"{API}/repos/o/r/commits/{SHA}/check-runs"
    pages = [
        {"total_count": 2, "check_runs": [_run("a", "completed", "success")]},
        {"total_count": 2, "check_runs": [_run("b", "completed", "failure")]},
    ]

    def runs(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        headers = {"link": f'<{runs_url}?page=2>; rel="next"'} if page == 1 else {}
        return httpx.Response(200, json=pages[page - 1], headers=headers)

    respx.get(runs_url).mock(side_effect=runs)
    respx.get(f"{API}/repos/o/r/commits/{SHA}/status").mock(
        return_value=httpx.Response(200, json={"state": "pending", "statuses": []})
    )

    status = await make_gateway(AuditLog()).ci_status(SHA)

    assert status.state == CiState.FAILURE
    assert [j.name for j in status.failed_jobs] == ["b"]


async def test_ci_status_rejects_non_sha_refs() -> None:
    with pytest.raises(PolicyViolation):
        await make_gateway(AuditLog()).ci_status("main")


@respx.mock
async def test_failed_job_logs_follow_redirect_without_leaking_auth() -> None:
    respx.get(f"{API}/repos/o/r/actions/jobs/1/logs").mock(
        return_value=httpx.Response(302, headers={"location": "https://blob.example/log1"})
    )
    blob = respx.get("https://blob.example/log1").mock(
        return_value=httpx.Response(200, content=b"head\n" + b"x" * 5000 + b"\xffTAIL")
    )
    respx.get(f"{API}/repos/o/r/actions/jobs/2/logs").mock(return_value=httpx.Response(410))

    logs = await make_gateway(AuditLog()).failed_job_logs([1, 2], 200)

    assert len(logs) <= 200
    assert "== job 1 ==" in logs
    assert "�TAIL" in logs
    assert "head" not in logs
    assert LOGS_UNAVAILABLE in logs
    assert "authorization" not in blob.calls[0].request.headers


def test_format_job_logs_respects_budget() -> None:
    logs = [(1, "a" * 100), (2, "b" * 100), (3, "c" * 100)]
    for max_chars in (0, 5, 30, 100, 1000):
        assert len(format_job_logs(logs, max_chars)) <= max_chars
    assert format_job_logs(logs, 1000).count("== job") == 3
