from collections.abc import Mapping, Sequence

from skeleton_crew.domain.ci_policy import BranchRule

REQUIRED_PERMISSIONS: Mapping[str, str] = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
    "actions": "read",
    "statuses": "read",
    "metadata": "read",
}
RULESETS_UNAVAILABLE = "rulesets are unavailable for this repository"
MISSING_RULESET_ID = "pull request rule without a ruleset id"
_LEVELS = {"read": 1, "write": 2, "admin": 3}
_UNPROTECTED_HINT = "enable 'Allow unprotected base branch' to run without one"


def _approval_count(parameters: Mapping[str, object]) -> int:
    if not isinstance(parameters, Mapping):
        return 0
    count = parameters.get("required_approving_review_count")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def approving_rulesets(rules: Sequence[BranchRule]) -> list[int | None]:
    return list(
        dict.fromkeys(
            r.ruleset_id
            for r in rules
            if r.type == "pull_request" and _approval_count(r.parameters) >= 1
        )
    )


def no_approval_problem(base_branch: str) -> str:
    return f"no ruleset on {base_branch!r} requires a pull request with at least 1 approval"


def unprotected_problems(problem: str, *, allow_unprotected: bool) -> list[str]:
    return [] if allow_unprotected else [f"{problem}; {_UNPROTECTED_HINT}"]


def bypass_problem(ruleset_id: int, bypass: str) -> str | None:
    if bypass == "never":
        return None
    return f"the App can bypass ruleset {ruleset_id} ({bypass or 'unknown'})"


def permission_problems(permissions: Mapping[str, str]) -> list[str]:
    problems = [
        f"App permission {name} must be at least {level}"
        for name, level in REQUIRED_PERMISSIONS.items()
        if _LEVELS.get(permissions.get(name, ""), 0) < _LEVELS[level]
    ]
    if "workflows" in permissions:
        problems.append("App must not have the workflows permission")
    if _LEVELS.get(permissions.get("administration", ""), 0) >= _LEVELS["write"]:
        problems.append("App must not have administration write")
    return problems
