import json

import httpx
import pytest
import respx
from github_helpers import API, SHA, TOKEN, AuditLog, make_gateway
from githubkit.exception import RequestFailed

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import LabelChange
from skeleton_crew.domain.text_policy import TRUNCATION_SUFFIX
from skeleton_crew.github.gateway import ReviewCheck

ISSUE = f"{API}/repos/o/r/issues/4"
ALLOWED = (AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED)
BLOCKED = (AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED)
DRY = (AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN)


def _change(add: set[str], remove: set[str]) -> LabelChange:
    return LabelChange(4, frozenset(add), frozenset(remove))


@respx.mock
async def test_escalation_label_change_adds_hitl_and_removes_ready() -> None:
    add = respx.post(f"{ISSUE}/labels").mock(return_value=httpx.Response(200, json=[]))
    remove = respx.delete(f"{ISSUE}/labels/skeleton").mock(
        return_value=httpx.Response(404, json={"message": "Label does not exist"})
    )
    audit = AuditLog()

    await make_gateway(audit).change_labels(_change({"skeleton HITL"}, {"skeleton"}))

    assert json.loads(add.calls[0].request.content) == {"labels": ["skeleton HITL"]}
    assert remove.called
    assert audit.kinds() == [ALLOWED]


@respx.mock
async def test_removing_label_with_space_is_url_encoded() -> None:
    route = respx.delete(f"{ISSUE}/labels/skeleton%20refine").mock(
        return_value=httpx.Response(200, json=[])
    )

    await make_gateway(AuditLog()).change_labels(_change(set(), {"skeleton refine"}))

    assert route.called


@pytest.mark.parametrize(
    ("add", "remove"),
    [
        ({"skeleton"}, set()),
        ({"Skeleton Refine"}, set()),
        ({"bug"}, set()),
        (set(), {"skeleton HITL"}),
        (set(), {"bug"}),
        ({"skeleton HITL", "skeleton"}, {"skeleton refine"}),
        (set(), set()),
    ],
)
@respx.mock(assert_all_mocked=True)
async def test_forbidden_label_changes_are_audited_and_never_sent(
    add: set[str], remove: set[str]
) -> None:
    audit = AuditLog()

    with pytest.raises(PolicyViolation):
        await make_gateway(audit).change_labels(_change(add, remove))

    assert audit.kinds() == [BLOCKED]
    assert not respx.calls


@respx.mock(assert_all_mocked=True)
async def test_dry_run_sends_nothing_for_any_write() -> None:
    audit = AuditLog()
    gateway = make_gateway(audit, dry_run=True)

    await gateway.change_labels(_change({"skeleton HITL"}, {"skeleton"}))
    await gateway.comment(4, "hello")
    assert await gateway.create_hitl_issue("Found bug", "details") is None
    assert await gateway.open_pr(branch="skeleton/4-fix", title="[skeleton] Fix", body="b") is None
    await gateway.set_review_check(sha=SHA, state=ReviewCheck.PENDING, summary="wait")
    await gateway.set_review_check(sha=SHA, state=ReviewCheck.PASSED, summary="ok")

    assert not respx.calls
    assert audit.kinds() == [DRY] * 6


@respx.mock
async def test_comment_is_truncated_to_max_comment_chars() -> None:
    route = respx.post(f"{ISSUE}/comments").mock(return_value=httpx.Response(201, json={}))
    audit = AuditLog()

    await make_gateway(audit, max_comment_chars=300).comment(4, "x" * 1000)

    sent = json.loads(route.calls[0].request.content)["body"]
    assert len(sent) == 300
    assert sent.endswith(TRUNCATION_SUFFIX)
    payload = audit.events[0][3]
    assert payload is not None
    assert len(str(payload["body"])) == 200


@respx.mock
async def test_create_hitl_issue_sets_exactly_the_hitl_label() -> None:
    route = respx.post(f"{API}/repos/o/r/issues").mock(
        return_value=httpx.Response(201, json={"number": 99})
    )

    number = await make_gateway(AuditLog(), max_comment_chars=200).create_hitl_issue(
        "Found", "y" * 500
    )

    body = json.loads(route.calls[0].request.content)
    assert number == 99
    assert body["labels"] == ["skeleton HITL"]
    assert len(body["body"]) == 200


@pytest.mark.parametrize("title", ["", "   ", "a" * 257, "two\nlines"])
@respx.mock(assert_all_mocked=True)
async def test_create_hitl_issue_rejects_bad_titles(title: str) -> None:
    audit = AuditLog()

    with pytest.raises(PolicyViolation):
        await make_gateway(audit).create_hitl_issue(title, "body")

    assert audit.kinds() == [BLOCKED]


@respx.mock
async def test_open_pr_targets_base_branch() -> None:
    route = respx.post(f"{API}/repos/o/r/pulls").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 5,
                "html_url": "https://github.com/o/r/pull/5",
                "head": {"ref": "skeleton/4-fix", "sha": SHA},
            },
        )
    )

    pr = await make_gateway(AuditLog()).open_pr(
        branch="skeleton/4-fix", title="t" * 256, body="Closes #4"
    )

    sent = json.loads(route.calls[0].request.content)
    assert (sent["head"], sent["base"], sent["draft"]) == ("skeleton/4-fix", "main", False)
    assert pr is not None
    assert pr.number == 5


@pytest.mark.parametrize(
    ("branch", "title"),
    [
        ("main", "t"),
        ("feature/x", "t"),
        ("skeleton/main", "t"),
        ("refs/heads/skeleton/4-fix", "t"),
        ("skeleton/4-fix", "t" * 257),
    ],
)
@respx.mock(assert_all_mocked=True)
async def test_open_pr_blocks_bad_branch_or_title(branch: str, title: str) -> None:
    audit = AuditLog()

    with pytest.raises(PolicyViolation):
        await make_gateway(audit).open_pr(branch=branch, title=title, body="b")

    assert audit.kinds() == [BLOCKED]


@pytest.mark.parametrize(
    ("state", "conclusion", "title"),
    [
        (ReviewCheck.PASSED, "success", "Review passed"),
        (ReviewCheck.CHANGES_REQUESTED, "failure", "Changes requested"),
    ],
)
@respx.mock
async def test_review_check_is_completed_with_conclusion(
    state: ReviewCheck, conclusion: str, title: str
) -> None:
    route = respx.post(f"{API}/repos/o/r/check-runs").mock(
        return_value=httpx.Response(201, json={})
    )

    await make_gateway(AuditLog()).set_review_check(sha=SHA, state=state, summary="fix x")

    sent = json.loads(route.calls[0].request.content)
    assert sent["name"] == "skeleton-crew/review"
    assert (sent["status"], sent["conclusion"]) == ("completed", conclusion)
    assert sent["output"] == {"title": title, "summary": "fix x"}


@respx.mock
async def test_pending_review_check_is_in_progress_without_conclusion_and_audited() -> None:
    route = respx.post(f"{API}/repos/o/r/check-runs").mock(
        return_value=httpx.Response(201, json={})
    )
    audit = AuditLog()

    await make_gateway(audit).set_review_check(sha=SHA, state=ReviewCheck.PENDING, summary="w")

    sent = json.loads(route.calls[0].request.content)
    assert sent["name"] == "skeleton-crew/review"
    assert sent["head_sha"] == SHA
    assert sent["status"] == "in_progress"
    assert "conclusion" not in sent
    assert sent["output"] == {"title": "Waiting for review", "summary": "w"}
    assert audit.kinds() == [ALLOWED]
    payload = audit.events[0][3]
    assert payload is not None
    assert payload["state"] == "pending"


@pytest.mark.parametrize("state", list(ReviewCheck))
@respx.mock(assert_all_mocked=True)
async def test_review_check_rejects_non_sha(state: ReviewCheck) -> None:
    audit = AuditLog()

    with pytest.raises(PolicyViolation):
        await make_gateway(audit).set_review_check(sha="HEAD", state=state, summary="s")

    assert audit.kinds() == [BLOCKED]


@respx.mock
async def test_failed_write_is_audited_and_reraised() -> None:
    respx.post(f"{ISSUE}/comments").mock(return_value=httpx.Response(500))
    audit = AuditLog()

    with pytest.raises(RequestFailed):
        await make_gateway(audit).comment(4, "hello")

    assert audit.kinds() == [ALLOWED]
    assert audit.events[0][2] == "comment failed"


@respx.mock
async def test_audit_payloads_never_contain_the_git_token() -> None:
    respx.post(f"{ISSUE}/comments").mock(return_value=httpx.Response(201, json={}))
    audit = AuditLog()

    await make_gateway(audit).comment(4, "hello")

    assert TOKEN not in repr(audit.events)
