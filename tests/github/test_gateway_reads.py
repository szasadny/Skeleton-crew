import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from ci_payloads import check_run, workflow_run
from github_helpers import API, SHA, AuditLog, make_gateway

from skeleton_crew.domain.ci_policy import CiState
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.github.gateway import LOGS_UNAVAILABLE

ISSUES = f"{API}/repos/o/r/issues"
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


def _pull(
    number: int, ref: str, *, head_repo: int | None = 1, **extra: object
) -> dict[str, object]:
    repo = None if head_repo is None else {"id": head_repo}
    return {
        "number": number,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": ref, "sha": SHA, "repo": repo},
        "base": {"ref": "main", "repo": {"id": 1}},
        **extra,
    }


@respx.mock
async def test_open_skeleton_prs_lists_once_and_ignores_forks_and_other_branches() -> None:
    route = _paged(
        [
            [_pull(10, "skeleton/5-fix", head_repo=2), _pull(11, "skeleton/6-other")],
            [_pull(12, "skeleton/5-fix", head_repo=None), _pull(13, "skeleton/5-fix")],
            [_pull(14, "feature/5-fix"), _pull(15, "skeleton/6-again")],
        ],
        f"{API}/repos/o/r/pulls",
    )

    prs = await make_gateway(AuditLog()).open_skeleton_prs()

    assert {n: [p.number for p in pulls] for n, pulls in prs.items()} == {5: [13], 6: [11, 15]}
    assert prs[5][0].head_sha == SHA
    assert route.call_count == 3
    assert route.calls[0].request.url.params["state"] == "open"


@respx.mock
async def test_closed_unmerged_pr_ignores_merged_pulls() -> None:
    route = _paged(
        [
            [
                _pull(2, "skeleton/5-fix", state="open", merged_at=None),
                _pull(3, "skeleton/5-fix", state="closed", merged_at="2026-01-01T00:00:00Z"),
            ],
            [_pull(4, "skeleton/5-fix", state="closed", merged_at=None)],
        ],
        f"{API}/repos/o/r/pulls",
    )

    pr = await make_gateway(AuditLog()).closed_unmerged_pr_for("skeleton/5-fix")

    assert pr is not None
    assert pr.number == 4
    params = route.calls[0].request.url.params
    assert (params["state"], params["head"]) == ("all", "o:skeleton/5-fix")


@respx.mock
async def test_only_merged_closed_pulls_mean_no_closed_pr() -> None:
    _paged(
        [
            [
                _pull(2, "skeleton/5-fix", state="open", merged_at=None),
                _pull(3, "skeleton/5-fix", state="closed", merged_at="2026-01-01T00:00:00Z"),
            ]
        ],
        f"{API}/repos/o/r/pulls",
    )

    assert await make_gateway(AuditLog()).closed_unmerged_pr_for("skeleton/5-fix") is None


@respx.mock
async def test_closed_pr_with_another_head_is_ignored() -> None:
    _paged(
        [[_pull(3, "skeleton/5-other", state="closed", merged_at=None)]],
        f"{API}/repos/o/r/pulls",
    )

    assert await make_gateway(AuditLog()).closed_unmerged_pr_for("skeleton/5-fix") is None


@pytest.mark.parametrize("branch", ["main", "feature/x", "skeleton/5-fix:main", "o:skeleton/5-x"])
@respx.mock
async def test_closed_pr_lookup_rejects_non_skeleton_branches(branch: str) -> None:
    route = respx.get(f"{API}/repos/o/r/pulls")

    with pytest.raises(PolicyViolation):
        await make_gateway(AuditLog()).closed_unmerged_pr_for(branch)
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (None, None), ("true", None), (1, None), (0, None)],
)
@respx.mock
async def test_pr_mergeable_is_strictly_a_bool(value: object, expected: bool | None) -> None:
    respx.get(f"{API}/repos/o/r/pulls/7").mock(
        return_value=httpx.Response(200, json=_pull(7, "skeleton/5-fix", mergeable=value))
    )

    assert await make_gateway(AuditLog()).pr_mergeable(7) is expected


@respx.mock
async def test_pr_mergeable_missing_field_is_unknown() -> None:
    respx.get(f"{API}/repos/o/r/pulls/7").mock(
        return_value=httpx.Response(200, json=_pull(7, "skeleton/5-fix"))
    )

    assert await make_gateway(AuditLog()).pr_mergeable(7) is None


@respx.mock
async def test_ci_status_paginates_check_runs_and_statuses() -> None:
    runs_url = f"{API}/repos/o/r/commits/{SHA}/check-runs"
    pages = [
        {"total_count": 2, "check_runs": [check_run("a", "completed", "success")]},
        {"total_count": 2, "check_runs": [check_run("b", "completed", "failure")]},
    ]

    def runs(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        headers = {"link": f'<{runs_url}?page=2>; rel="next"'} if page == 1 else {}
        return httpx.Response(200, json=pages[page - 1], headers=headers)

    respx.get(runs_url).mock(side_effect=runs)
    respx.get(f"{API}/repos/o/r/commits/{SHA}/status").mock(
        return_value=httpx.Response(200, json={"state": "pending", "statuses": []})
    )
    workflows = respx.get(f"{API}/repos/o/r/actions/runs", params={"head_sha": SHA}).mock(
        return_value=httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
    )

    status = await make_gateway(AuditLog()).ci_status(SHA)

    assert status.state == CiState.FAILURE
    assert [j.name for j in status.failed_jobs] == ["b"]
    assert workflows.called


@respx.mock
async def test_ci_status_waits_for_workflow_runs_and_required_checks() -> None:
    respx.get(f"{API}/repos/o/r/commits/{SHA}/check-runs").mock(
        return_value=httpx.Response(
            200, json={"total_count": 1, "check_runs": [check_run("lint", "completed", "success")]}
        )
    )
    respx.get(f"{API}/repos/o/r/commits/{SHA}/status").mock(
        return_value=httpx.Response(200, json={"state": "success", "statuses": []})
    )
    respx.get(f"{API}/repos/o/r/actions/runs", params={"head_sha": SHA}).mock(
        return_value=httpx.Response(
            200,
            json={"total_count": 1, "workflow_runs": [workflow_run("CI", "completed", "success")]},
        )
    )
    gateway = make_gateway(AuditLog())

    assert (await gateway.ci_status(SHA)).state == CiState.SUCCESS
    assert (await gateway.ci_status(SHA, {"lint", "tests"})).state == CiState.PENDING


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


@respx.mock
@pytest.mark.parametrize(("private", "expected"), [(True, True), (False, False), ("yes", False)])
async def test_repo_is_private_only_for_a_literal_true(private: object, expected: bool) -> None:
    respx.get(f"{API}/repos/o/r").mock(
        return_value=httpx.Response(200, json={"id": 1, "private": private})
    )

    assert await make_gateway(AuditLog()).repo_is_private() is expected


@respx.mock
@pytest.mark.parametrize(
    ("queued", "running", "active"),
    [
        (["main"], ["feature/x"], False),
        (["main"], ["skeleton/12-fix-it"], True),
        (["skeleton/12-fix-it"], [], True),
        (["skeleton/main"], ["skeleton/"], False),
        ([], [], False),
    ],
)
async def test_skeleton_ci_active_counts_only_skeleton_branches(
    queued: list[str], running: list[str], active: bool
) -> None:
    for status, branches in (("queued", queued), ("in_progress", running)):
        runs = [{"head_branch": b, "status": status} for b in branches]
        respx.get(f"{API}/repos/o/r/actions/runs", params={"status": status}).mock(
            return_value=httpx.Response(200, json={"total_count": len(runs), "workflow_runs": runs})
        )

    assert await make_gateway(AuditLog()).skeleton_ci_active() is active
