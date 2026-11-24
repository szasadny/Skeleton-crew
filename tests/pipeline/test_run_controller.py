import asyncio

import pytest
from factories import seed_refine, seed_run, seed_task
from fake_agent import limited_outcome
from git_helpers import git
from pipeline_helpers import (
    BOTH,
    HELLO,
    HI,
    ONE_LINER,
    Harness,
    advance_main,
    assert_no_forbidden_writes,
    only_task,
    open_pr,
    parents_of,
    refine_json,
    review_json,
    selection_json,
    worker_json,
    workspace_editor,
)

from skeleton_crew.agents.base import failed_outcome
from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.ci_policy import CiState, CiStatus
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import (
    RefineState,
    RunState,
    RunStep,
    RunTrigger,
    TaskKind,
    TaskState,
)
from skeleton_crew.pipeline.run_controller import RunController
from skeleton_crew.pipeline.run_flow import USAGE_LIMIT_NOTE
from skeleton_crew.runtime.containers import MANAGED_LABEL, MANAGED_VALUE, ContainerHandle
from skeleton_crew.store import runs_store
from skeleton_crew.web.ports import RunControl


def test_run_controller_satisfies_run_control(harness: Harness) -> None:
    control: RunControl = harness.controller
    assert control.is_running is False


async def test_step_shows_the_selector_only_while_it_runs(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(ONE_LINER)]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())
    steps: dict[str, RunStep | None] = {}
    harness.on_session(lambda call: steps.setdefault(call.argv[1], harness.controller.step))

    await harness.run()

    assert steps == {"selector": RunStep.SELECTOR, "worker": None, "reviewer": None}
    assert harness.controller.step is None


async def test_dry_run_makes_no_github_writes(harness: Harness) -> None:
    harness.set(dry_run=True)
    harness.add_ready_issue(1)
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.patches = [harness.patch(ONE_LINER)]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json(new_issues=[{"title": "T", "body": "B"}]))
    harness.backend.script(Role.REVIEWER, review_json())
    harness.backend.script(Role.REFINER, refine_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.writes == []
    assert harness.gateway.pushes == []
    assert git(harness.remote, "for-each-ref").strip() == b""
    dry = [k for k, o in await harness.audit_kinds() if o is AuditOutcome.DRY_RUN]
    assert len(dry) == 7
    assert (await harness.run_row(run_id)).dry_run is True


async def test_failed_selection_still_runs_pr_maintenance(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Add farewell")
    head = await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.SELECTOR, selection_json([2, 99]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.FAILED
    assert "selection failed" in str(run.note)
    task = await only_task(harness, run_id)
    assert (task.issue_number, task.kind, task.state) == (1, TaskKind.PR_FIX, TaskState.DONE)
    [(_, merged)] = harness.gateway.pushes
    assert parents_of(harness, merged)[0] == head
    assert_no_forbidden_writes(harness)


async def test_kill_switch_refuses_to_start(harness: Harness) -> None:
    harness.set(kill_switch=True)
    await harness.save_settings()

    assert await harness.controller.start(RunTrigger.MANUAL) is None
    assert not harness.controller.is_running


async def test_quota_counts_tasks_from_earlier_runs_today(harness: Harness) -> None:
    harness.set("limits", issues_per_day=1)
    harness.add_ready_issue(1)
    async with harness.sessionmaker.begin() as session:
        run = await seed_run(session)
        await seed_task(session, run.id, 7, "Earlier")
        await runs_store.finish_run(session, run.id, RunState.COMPLETED)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.backend.requests == []


async def test_task_aborted_by_the_usage_limit_leaves_the_quota_free(harness: Harness) -> None:
    harness.set("limits", issues_per_day=1)
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(ONE_LINER)]
    harness.backend.script(Role.SELECTOR, selection_json([1]), selection_json([1]))
    harness.backend.script(Role.WORKER, limited_outcome(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    first = await harness.run()
    second = await harness.run()

    aborted = await only_task(harness, first)
    assert aborted.state is TaskState.ABORTED
    task = await only_task(harness, second)
    assert (task.issue_number, task.state) == (1, TaskState.DONE)


@pytest.mark.parametrize("final", [TaskState.ESCALATED, TaskState.DONE])
async def test_escalated_or_done_task_uses_the_quota(harness: Harness, final: TaskState) -> None:
    harness.set("limits", issues_per_day=1)
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Second")
    harness.patches = [harness.patch(ONE_LINER)]
    harness.backend.script(Role.SELECTOR, selection_json([1]), selection_json([2]))
    if final is TaskState.DONE:
        harness.backend.script(Role.WORKER, worker_json(), worker_json())
        harness.backend.script(Role.REVIEWER, review_json(), review_json())
    else:
        harness.backend.script(Role.WORKER, failed_outcome("session crashed"), worker_json())

    first = await harness.run()
    second = await harness.run()

    task = await only_task(harness, first)
    assert task.state is final
    assert await harness.tasks(second) == []
    assert len(harness.backend.prompts(Role.SELECTOR)) == 1
    assert len(harness.backend.prompts(Role.WORKER)) == 1


async def test_kill_aborts_running_worker_and_removes_containers(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(ONE_LINER)]
    harness.gateway.ci_default = [CiStatus(CiState.PENDING, [])]
    harness.sleep_hook = asyncio.Event()
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    await harness.save_settings()

    run_id = await harness.controller.start(RunTrigger.MANUAL)
    assert run_id is not None
    assert await harness.controller.start(RunTrigger.MANUAL) is None
    await asyncio.wait_for(harness.sleeping.wait(), 10)
    assert harness.runtime.running
    await harness.controller.kill()

    assert not harness.controller.is_running
    assert not harness.runtime.running
    task = await only_task(harness, run_id)
    assert task.state is TaskState.ABORTED
    assert (await harness.run_row(run_id)).state is RunState.ABORTED
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr"]
    assert AuditKind.KILL_SWITCH not in {k for k, _ in await harness.audit_kinds()}


async def test_window_end_stops_new_workers(harness: Harness) -> None:
    harness.set("schedule", window_hours=0.1)
    harness.set("limits", max_parallel_workers=1, ci_poll_seconds=600)
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Second")
    harness.patches = [harness.patch(ONE_LINER)]
    harness.gateway.ci_default = [CiStatus(CiState.PENDING, []), CiStatus(CiState.SUCCESS, [])]
    harness.backend.script(Role.SELECTOR, selection_json([1, 2]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.issue_number, task.state) == (1, TaskState.DONE)
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED


async def test_usage_limit_stops_new_workers_without_escalating(harness: Harness) -> None:
    harness.set("limits", max_parallel_workers=1)
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Second")
    harness.backend.script(Role.SELECTOR, selection_json([1, 2]))
    harness.backend.script(Role.WORKER, limited_outcome(), worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.issue_number, task.state) == (1, TaskState.ABORTED)
    assert len(harness.backend.prompts(Role.WORKER)) == 1
    run = await harness.run_row(run_id)
    assert (run.state, run.note) == (RunState.FAILED, USAGE_LIMIT_NOTE)
    assert harness.gateway.writes == []
    assert harness.gateway.issues[1].labels == {"skeleton"}
    assert harness.gateway.issues[2].labels == {"skeleton"}


async def test_parallel_workers_and_refines_complete(harness: Harness) -> None:
    harness.set("limits", max_parallel_workers=2)
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Second")
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.patches = [harness.patch(ONE_LINER)]
    harness.backend.script(Role.SELECTOR, selection_json([2, 1]))
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json(), review_json())
    harness.backend.script(Role.REFINER, refine_json())

    run_id = await harness.run()

    tasks = await harness.tasks(run_id)
    assert [(t.issue_number, t.state) for t in tasks] == [
        (2, TaskState.DONE),
        (1, TaskState.DONE),
    ]
    [refine] = await harness.refines(run_id)
    assert refine.state is RefineState.DONE
    assert len(harness.writes("open_pr")) == 2
    assert not harness.runtime.running
    assert_no_forbidden_writes(harness)


async def test_reconcile_on_startup_aborts_leftovers_and_comments(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Dry")
    async with harness.sessionmaker.begin() as session:
        live = await seed_run(session, RunTrigger.SCHEDULE)
        await seed_task(session, live.id, 1, "Add greeting", state=TaskState.AWAITING_CI)
        refine = await seed_refine(session, live.id, 5)
        dry = await seed_run(session, dry_run=True)
        await seed_task(session, dry.id, 2, "Dry")
    harness.runtime.running["leftover"] = ContainerHandle("leftover", "skeleton-crew-x")
    harness.runtime.labels["leftover"] = {MANAGED_LABEL: MANAGED_VALUE}
    await harness.save_settings()
    controller = RunController(harness.deps())

    await controller.reconcile_on_startup()

    assert harness.runtime.running == {}
    assert [t.state for t in await harness.tasks(live.id)] == [TaskState.ABORTED]
    assert [t.state for t in await harness.tasks(dry.id)] == [TaskState.ABORTED]
    assert [r.id for r in await harness.refines(live.id)] == [refine.id]
    assert (await harness.refines(live.id))[0].state is RefineState.ABORTED
    assert (await harness.run_row(live.id)).state is RunState.ABORTED
    assert (await harness.run_row(dry.id)).state is RunState.ABORTED
    assert [w.args["issue"] for w in harness.writes("comment")] == [1]
    assert_no_forbidden_writes(harness)
