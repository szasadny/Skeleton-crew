from collections.abc import Mapping
from dataclasses import dataclass

from githubkit.exception import RequestFailed

from skeleton_crew.github.gateway import Gateway, RulesUnavailable

REQUIRED_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
    "actions": "read",
    "statuses": "read",
    "metadata": "read",
}
_LEVELS = {"read": 1, "write": 2, "admin": 3}


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    problems: list[str]


def _describe(exc: Exception) -> str:
    if isinstance(exc, RequestFailed):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _approval_count(parameters: Mapping[str, object]) -> int:
    if not isinstance(parameters, Mapping):
        return 0
    count = parameters.get("required_approving_review_count")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


_UNPROTECTED_HINT = "enable 'Allow unprotected base branch' to run without one"


async def _ruleset_problems(gw: Gateway, base_branch: str, allow_unprotected: bool) -> list[str]:
    try:
        rules = await gw.branch_rules(base_branch)
    except RulesUnavailable:
        if allow_unprotected:
            return []
        return [f"rulesets are unavailable for this repository; {_UNPROTECTED_HINT}"]
    except Exception as exc:
        return [f"cannot read rules for {base_branch!r}: {_describe(exc)}"]
    approving = [
        r for r in rules if r.type == "pull_request" and _approval_count(r.parameters) >= 1
    ]
    if not approving:
        if allow_unprotected:
            return []
        return [
            f"no ruleset on {base_branch!r} requires a pull request with at least 1 approval; "
            + _UNPROTECTED_HINT
        ]
    problems: list[str] = []
    for ruleset_id in dict.fromkeys(r.ruleset_id for r in approving):
        if ruleset_id is None:
            problems.append("pull request rule without a ruleset id")
            continue
        try:
            bypass = await gw.ruleset_bypass(ruleset_id)
        except Exception as exc:
            problems.append(f"cannot read ruleset {ruleset_id}: {_describe(exc)}")
            continue
        if bypass != "never":
            problems.append(f"the App can bypass ruleset {ruleset_id} ({bypass or 'unknown'})")
    return problems


async def _permission_problems(gw: Gateway) -> list[str]:
    try:
        permissions = await gw.installation_permissions()
    except Exception as exc:
        return [f"cannot read installation permissions: {_describe(exc)}"]
    problems: list[str] = []
    for name, level in REQUIRED_PERMISSIONS.items():
        if _LEVELS.get(permissions.get(name, ""), 0) < _LEVELS[level]:
            problems.append(f"App permission {name} must be at least {level}")
    if "workflows" in permissions:
        problems.append("App must not have the workflows permission")
    if _LEVELS.get(permissions.get("administration", ""), 0) >= _LEVELS["write"]:
        problems.append("App must not have administration write")
    return problems


async def check_repository(
    gw: Gateway, *, base_branch: str, allow_unprotected: bool = False
) -> PreflightReport:
    problems = await _ruleset_problems(gw, base_branch, allow_unprotected)
    problems += await _permission_problems(gw)
    return PreflightReport(ok=not problems, problems=problems)
