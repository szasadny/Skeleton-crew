from dataclasses import dataclass

from githubkit.exception import RequestFailed

from skeleton_crew.domain.repo_policy import (
    MISSING_RULESET_ID,
    RULESETS_UNAVAILABLE,
    approving_rulesets,
    bypass_problem,
    no_approval_problem,
    permission_problems,
    unprotected_problems,
)
from skeleton_crew.github.gateway import Gateway, RulesUnavailable


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    problems: list[str]


def _describe(exc: Exception) -> str:
    if isinstance(exc, RequestFailed):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


async def _ruleset_problems(gw: Gateway, base_branch: str, allow_unprotected: bool) -> list[str]:
    try:
        rules = await gw.branch_rules(base_branch)
    except RulesUnavailable:
        return unprotected_problems(RULESETS_UNAVAILABLE, allow_unprotected=allow_unprotected)
    except Exception as exc:
        return [f"cannot read rules for {base_branch!r}: {_describe(exc)}"]
    ruleset_ids = approving_rulesets(rules)
    if not ruleset_ids:
        unprotected = no_approval_problem(base_branch)
        return unprotected_problems(unprotected, allow_unprotected=allow_unprotected)
    problems: list[str] = []
    for ruleset_id in ruleset_ids:
        if ruleset_id is None:
            problems.append(MISSING_RULESET_ID)
            continue
        try:
            bypass = await gw.ruleset_bypass(ruleset_id)
        except Exception as exc:
            problems.append(f"cannot read ruleset {ruleset_id}: {_describe(exc)}")
            continue
        problem = bypass_problem(ruleset_id, bypass)
        if problem is not None:
            problems.append(problem)
    return problems


async def _permission_problems(gw: Gateway) -> list[str]:
    try:
        permissions = await gw.installation_permissions()
    except Exception as exc:
        return [f"cannot read installation permissions: {_describe(exc)}"]
    return permission_problems(permissions)


async def check_repository(
    gw: Gateway, *, base_branch: str, allow_unprotected: bool = False
) -> PreflightReport:
    problems = await _ruleset_problems(gw, base_branch, allow_unprotected)
    problems += await _permission_problems(gw)
    return PreflightReport(ok=not problems, problems=problems)
