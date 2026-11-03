import httpx
import pytest
import respx
from fake_gateway import FakeGitHubGateway
from github_helpers import API, AuditLog, make_gateway

from skeleton_crew.github.gateway import BranchRule, RulesUnavailable
from skeleton_crew.github.preflight import check_repository


async def _problems(gw: FakeGitHubGateway) -> list[str]:
    report = await check_repository(gw, base_branch="main")
    assert report.ok == (not report.problems)
    return report.problems


async def test_protected_repo_with_minimal_permissions_passes() -> None:
    assert await _problems(FakeGitHubGateway()) == []


@pytest.mark.parametrize("bypass", ["always", "pull_requests_only", "exempt", ""])
async def test_bypassable_ruleset_fails(bypass: str) -> None:
    gw = FakeGitHubGateway()
    gw.ruleset_bypasses[1] = bypass

    assert len(await _problems(gw)) == 1


async def test_any_bypassable_contributing_ruleset_fails() -> None:
    gw = FakeGitHubGateway()
    gw.rules.append(BranchRule("pull_request", 2, {"required_approving_review_count": 2}))
    gw.ruleset_bypasses[2] = "always"

    assert len(await _problems(gw)) == 1


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [BranchRule("pull_request", 1, {"required_approving_review_count": 0})],
        [BranchRule("pull_request", 1, {"required_approving_review_count": True})],
        [BranchRule("pull_request", 1, {"required_approving_review_count": "1"})],
        [BranchRule("pull_request", 1, {})],
        [BranchRule("required_signatures", 1, {"required_approving_review_count": 1})],
    ],
)
async def test_missing_approval_requirement_fails(rules: list[BranchRule]) -> None:
    gw = FakeGitHubGateway()
    gw.rules = rules

    assert len(await _problems(gw)) == 1


async def test_pull_request_rule_without_ruleset_id_fails() -> None:
    gw = FakeGitHubGateway()
    gw.rules = [BranchRule("pull_request", None, {"required_approving_review_count": 1})]

    assert len(await _problems(gw)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"workflows": "read"},
        {"workflows": "write"},
        {"administration": "write"},
        {"contents": "read"},
        {"checks": "read"},
    ],
)
async def test_wrong_installation_permissions_fail(change: dict[str, str]) -> None:
    gw = FakeGitHubGateway()
    gw.installation_perms |= change

    assert len(await _problems(gw)) == 1


async def test_missing_required_permission_fails() -> None:
    gw = FakeGitHubGateway()
    del gw.installation_perms["actions"]

    assert len(await _problems(gw)) == 1


async def test_administration_read_is_allowed() -> None:
    gw = FakeGitHubGateway()
    gw.installation_perms["administration"] = "read"

    assert await _problems(gw) == []


@pytest.mark.parametrize("method", ["branch_rules", "ruleset_bypass", "installation_permissions"])
async def test_api_errors_fail_closed(method: str) -> None:
    gw = FakeGitHubGateway()
    gw.failures[method] = RuntimeError("boom")

    assert len(await _problems(gw)) == 1


@respx.mock
async def test_rules_endpoint_error_through_real_gateway_fails_closed() -> None:
    respx.get(f"{API}/repos/o/r/rules/branches/main").mock(return_value=httpx.Response(403))
    respx.get(f"{API}/repos/o/r/installation").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1,
                "permissions": {
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                    "checks": "write",
                    "actions": "read",
                    "statuses": "read",
                    "metadata": "read",
                },
            },
        )
    )

    report = await check_repository(make_gateway(AuditLog()), base_branch="main")
    allowed = await check_repository(
        make_gateway(AuditLog()), base_branch="main", allow_unprotected=True
    )

    assert report.problems == [
        "rulesets are unavailable for this repository; "
        "enable 'Allow unprotected base branch' to run without one"
    ]
    assert allowed.ok


@respx.mock
async def test_real_gateway_reads_rules_and_bypass() -> None:
    respx.get(f"{API}/repos/o/r/rules/branches/main").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "type": "pull_request",
                    "ruleset_id": 9,
                    "parameters": {"required_approving_review_count": 1},
                }
            ],
        )
    )
    respx.get(f"{API}/repos/o/r/rulesets/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "current_user_can_bypass": "always"})
    )
    respx.get(f"{API}/repos/o/r/installation").mock(return_value=httpx.Response(500))

    report = await check_repository(make_gateway(AuditLog()), base_branch="main")

    assert report.problems == [
        "the App can bypass ruleset 9 (always)",
        "cannot read installation permissions: HTTP 500",
    ]


async def test_unprotected_repo_needs_explicit_opt_in() -> None:
    gw = FakeGitHubGateway()
    gw.rules = []

    blocked = await check_repository(gw, base_branch="main")
    allowed = await check_repository(gw, base_branch="main", allow_unprotected=True)

    assert not blocked.ok
    assert "Allow unprotected base branch" in blocked.problems[0]
    assert allowed.ok


async def test_unavailable_rulesets_need_explicit_opt_in() -> None:
    gw = FakeGitHubGateway()
    gw.failures["branch_rules"] = RulesUnavailable()

    assert not (await check_repository(gw, base_branch="main")).ok
    assert (await check_repository(gw, base_branch="main", allow_unprotected=True)).ok


@pytest.mark.parametrize("bypass", ["always", "pull_requests_only", "exempt"])
async def test_opt_in_never_accepts_a_bypassable_ruleset(bypass: str) -> None:
    gw = FakeGitHubGateway()
    gw.ruleset_bypasses[1] = bypass

    assert not (await check_repository(gw, base_branch="main", allow_unprotected=True)).ok


async def test_opt_in_still_checks_app_permissions() -> None:
    gw = FakeGitHubGateway()
    gw.rules = []
    gw.installation_perms["workflows"] = "write"

    report = await check_repository(gw, base_branch="main", allow_unprotected=True)

    assert report.problems == ["App must not have the workflows permission"]
