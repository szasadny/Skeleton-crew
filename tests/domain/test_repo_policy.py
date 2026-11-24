import pytest

from skeleton_crew.domain.ci_policy import BranchRule
from skeleton_crew.domain.repo_policy import (
    MISSING_RULESET_ID,
    REQUIRED_PERMISSIONS,
    RULESETS_UNAVAILABLE,
    approving_rulesets,
    bypass_problem,
    no_approval_problem,
    permission_problems,
    unprotected_problems,
)

HINT = "enable 'Allow unprotected base branch' to run without one"


def _pr_rule(ruleset_id: int | None, count: object = 1) -> BranchRule:
    return BranchRule("pull_request", ruleset_id, {"required_approving_review_count": count})


def test_approving_rulesets_are_deduplicated_in_order() -> None:
    rules = [_pr_rule(2), _pr_rule(1), _pr_rule(2, 3), _pr_rule(None)]
    assert approving_rulesets(rules) == [2, 1, None]


@pytest.mark.parametrize(
    "rule",
    [
        _pr_rule(1, 0),
        _pr_rule(1, True),
        _pr_rule(1, "1"),
        BranchRule("pull_request", 1, {}),
        BranchRule("required_signatures", 1, {"required_approving_review_count": 1}),
    ],
)
def test_rules_without_a_real_approval_count_do_not_protect(rule: BranchRule) -> None:
    assert approving_rulesets([rule]) == []


def test_unprotected_problems_name_the_way_out_unless_allowed() -> None:
    assert unprotected_problems(RULESETS_UNAVAILABLE, allow_unprotected=True) == []
    assert unprotected_problems(RULESETS_UNAVAILABLE, allow_unprotected=False) == [
        f"rulesets are unavailable for this repository; {HINT}"
    ]
    assert unprotected_problems(no_approval_problem("main"), allow_unprotected=False) == [
        f"no ruleset on 'main' requires a pull request with at least 1 approval; {HINT}"
    ]
    assert MISSING_RULESET_ID == "pull request rule without a ruleset id"


@pytest.mark.parametrize(
    ("bypass", "expected"),
    [
        ("never", None),
        ("always", "the App can bypass ruleset 3 (always)"),
        ("pull_requests_only", "the App can bypass ruleset 3 (pull_requests_only)"),
        ("", "the App can bypass ruleset 3 (unknown)"),
    ],
)
def test_any_bypass_other_than_never_is_a_problem(bypass: str, expected: str | None) -> None:
    assert bypass_problem(3, bypass) == expected


def test_required_permissions_pass() -> None:
    assert permission_problems(REQUIRED_PERMISSIONS) == []
    assert permission_problems({**REQUIRED_PERMISSIONS, "contents": "admin"}) == []


def test_permission_problems_are_reported_in_order() -> None:
    permissions = {
        **REQUIRED_PERMISSIONS,
        "contents": "read",
        "checks": "none",
        "workflows": "read",
        "administration": "write",
    }
    assert permission_problems(permissions) == [
        "App permission contents must be at least write",
        "App permission checks must be at least write",
        "App must not have the workflows permission",
        "App must not have administration write",
    ]


def test_missing_permissions_are_problems() -> None:
    assert len(permission_problems({})) == len(REQUIRED_PERMISSIONS)
