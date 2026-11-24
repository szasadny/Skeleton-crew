from dataclasses import dataclass

from skeleton_crew.domain.settings import Role


@dataclass(frozen=True)
class SessionBudget:
    max_turns: int
    timeout_s: float
    max_budget_usd: float | None


def workspace_writable(role: Role) -> bool:
    return role is Role.WORKER


def may_continue(stopped_early: str | None, session_id: str | None, usage_limited: bool) -> bool:
    return stopped_early is not None and session_id is not None and not usage_limited


def remaining_budget(
    budget: SessionBudget, cost_usd: float, turns: int, elapsed_s: float
) -> SessionBudget | None:
    max_turns = budget.max_turns - turns
    timeout_s = budget.timeout_s - elapsed_s
    money = None if budget.max_budget_usd is None else budget.max_budget_usd - cost_usd
    if max_turns < 1 or timeout_s <= 0 or (money is not None and money <= 0):
        return None
    return SessionBudget(max_turns, timeout_s, money)
