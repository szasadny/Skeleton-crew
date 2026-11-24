import json
from dataclasses import replace

import pytest
from fake_agent import limited_outcome, ok_outcome, stopped_outcome
from pipeline_helpers import (
    GREETING,
    Harness,
    assert_no_forbidden_writes,
    only_task,
    review_json,
    worker_json,
)

from skeleton_crew.agents.base import failed_outcome
from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.ci_policy import ReviewCheck
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.pipeline.outcomes import USAGE_LIMIT_REASON

pytestmark = pytest.mark.usefixtures("selected_issue")


@pytest.mark.parametrize(
    "output",
    ["{not json", json.dumps({"status": "done"}), worker_json(pr_title="x" * 80)],
)
async def test_invalid_worker_output_escalates(harness: Harness, output: str) -> None:
    harness.backend.script(Role.WORKER, output)

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "invalid WorkerResult" in str(task.note)
    assert harness.actions() == ["comment", "change_labels"]


async def test_failed_session_escalates(harness: Harness) -> None:
    harness.backend.script(Role.WORKER, failed_outcome("session ended with error_max_turns"))

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "error_max_turns" in str(task.note)
    assert len(harness.backend.prompts(Role.WORKER)) == 1


async def test_usage_limit_in_the_worker_aborts_without_escalating(harness: Harness) -> None:
    harness.backend.script(Role.WORKER, limited_outcome(), worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.note) == (TaskState.ABORTED, USAGE_LIMIT_REASON)
    assert task.cost_usd == pytest.approx(0.03)
    assert len(harness.backend.prompts(Role.WORKER)) == 1
    assert harness.gateway.writes == []
    assert harness.gateway.issues[1].labels == {"skeleton"}
    assert AuditKind.TASK_ESCALATED not in {k for k, _ in await harness.audit_kinds()}
    assert not harness.runtime.running


async def test_usage_limit_in_the_reviewer_leaves_the_review_check_pending(
    harness: Harness,
) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, limited_outcome(session_id="review"), review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.note) == (TaskState.ABORTED, USAGE_LIMIT_REASON)
    assert task.cost_usd == pytest.approx(0.04)
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr"]
    checks = [(w.args["sha"], w.args["state"]) for w in harness.writes("set_review_check")]
    assert checks == [(task.head_sha, ReviewCheck.PENDING)]
    assert harness.gateway.review_checks[str(task.head_sha)][0] is ReviewCheck.PENDING
    assert harness.gateway.issues[1].labels == {"skeleton"}
    assert not harness.runtime.running


@pytest.mark.parametrize("role", [Role.WORKER, Role.REVIEWER])
async def test_usage_limit_with_a_valid_output_is_never_a_result(
    harness: Harness, role: Role
) -> None:
    harness.patches = [harness.patch(GREETING)]
    output = worker_json() if role is Role.WORKER else review_json()
    harness.backend.script(role, replace(ok_outcome(output), usage_limited=True))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.note) == (TaskState.ABORTED, USAGE_LIMIT_REASON)
    states = [w.args["state"] for w in harness.writes("set_review_check")]
    assert set(states) <= {ReviewCheck.PENDING}
    assert "change_labels" not in harness.actions()


async def test_usage_limited_early_stop_is_not_continued(harness: Harness) -> None:
    harness.backend.script(Role.WORKER, replace(stopped_outcome(), usage_limited=True))
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ABORTED
    assert len(harness.backend.prompts(Role.WORKER)) == 1
    assert harness.gateway.writes == []


async def test_hook_stop_is_continued_and_the_worker_finishes(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, stopped_outcome(), stopped_outcome(), worker_json())
    harness.backend.script(Role.REVIEWER, stopped_outcome(session_id="review"), review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr", "set_review_check"]
    first, *continues = harness.backend.prompts(Role.WORKER)
    assert first.resume_session_id is None
    assert len(continues) == 2
    for request in continues:
        assert request.resume_session_id == "worker-session"
        assert request.prompt.startswith("# Task: continue")
    assert harness.backend.prompts(Role.REVIEWER)[1].resume_session_id == "review"
    assert task.cost_usd == pytest.approx(0.05)
    assert_no_forbidden_writes(harness)


async def test_continues_are_capped_then_escalate(harness: Harness) -> None:
    harness.set("limits", max_continues=2)
    harness.backend.script(Role.WORKER, *(stopped_outcome() for _ in range(4)))

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "stopped early: hook_stopped" in str(task.note)
    assert len(harness.backend.prompts(Role.WORKER)) == 3
    assert harness.actions() == ["comment", "change_labels"]


async def test_continues_get_only_what_is_left_of_the_session_limits(harness: Harness) -> None:
    harness.set("limits", max_turns=10, session_timeout_minutes=10, max_budget_usd_per_session=1.0)
    harness.session_seconds = 120.0
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, stopped_outcome(), stopped_outcome(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    first, second, third = harness.backend.prompts(Role.WORKER)
    assert [r.max_turns for r in (first, second, third)] == [10, 8, 6]
    assert [r.timeout_s for r in (first, second, third)] == [600.0, 480.0, 360.0]
    assert second.max_budget_usd == pytest.approx(0.99)
    assert third.max_budget_usd == pytest.approx(0.98)


@pytest.mark.parametrize(
    ("limits", "session_seconds"),
    [
        ({"max_turns": 10}, 0.0),
        ({"session_timeout_minutes": 5}, 300.0),
        ({"max_budget_usd_per_session": 0.01}, 0.0),
    ],
)
async def test_no_continue_once_the_session_limits_are_used_up(
    harness: Harness, limits: dict[str, float], session_seconds: float
) -> None:
    harness.set("limits", max_continues=5, **limits)
    harness.session_seconds = session_seconds
    used_up = replace(stopped_outcome(), turns=10)
    harness.backend.script(Role.WORKER, used_up, worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "stopped early: hook_stopped" in str(task.note)
    assert len(harness.backend.prompts(Role.WORKER)) == 1


@pytest.mark.parametrize(("max_continues", "session_id"), [(0, "worker-session"), (5, None)])
async def test_early_stop_is_not_continued_without_budget_or_session(
    harness: Harness, max_continues: int, session_id: str | None
) -> None:
    harness.set("limits", max_continues=max_continues)
    harness.backend.script(Role.WORKER, stopped_outcome(session_id=session_id), worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert len(harness.backend.prompts(Role.WORKER)) == 1
