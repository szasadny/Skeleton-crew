import pytest

from skeleton_crew.domain.session_policy import (
    SessionBudget,
    may_continue,
    remaining_budget,
    workspace_writable,
)
from skeleton_crew.domain.settings import Role

BUDGET = SessionBudget(max_turns=10, timeout_s=600.0, max_budget_usd=1.0)


def test_only_the_worker_gets_a_writable_workspace() -> None:
    assert [r for r in Role if workspace_writable(r)] == [Role.WORKER]


def test_a_stopped_session_with_an_id_may_continue() -> None:
    assert may_continue("hook_stopped", "s-1", usage_limited=False)


@pytest.mark.parametrize(
    ("stopped_early", "session_id", "usage_limited"),
    [(None, "s-1", False), ("hook_stopped", None, False), ("hook_stopped", "s-1", True)],
)
def test_no_continue_without_an_early_stop_an_id_or_under_the_usage_limit(
    stopped_early: str | None, session_id: str | None, usage_limited: bool
) -> None:
    assert not may_continue(stopped_early, session_id, usage_limited)


def test_a_continue_gets_only_what_is_left() -> None:
    left = remaining_budget(BUDGET, cost_usd=0.25, turns=4, elapsed_s=120.0)
    assert left == SessionBudget(max_turns=6, timeout_s=480.0, max_budget_usd=0.75)


def test_no_budget_cap_stays_uncapped() -> None:
    uncapped = SessionBudget(max_turns=10, timeout_s=600.0, max_budget_usd=None)
    left = remaining_budget(uncapped, cost_usd=5.0, turns=1, elapsed_s=1.0)
    assert left is not None
    assert left.max_budget_usd is None


@pytest.mark.parametrize(
    ("cost_usd", "turns", "elapsed_s"),
    [(0.0, 10, 0.0), (0.0, 11, 0.0), (0.0, 0, 600.0), (1.0, 0, 0.0), (2.0, 0, 0.0)],
)
def test_a_used_up_limit_allows_no_continue(cost_usd: float, turns: int, elapsed_s: float) -> None:
    assert remaining_budget(BUDGET, cost_usd, turns, elapsed_s) is None
