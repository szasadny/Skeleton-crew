import asyncio
from pathlib import Path

import pytest
from fake_gateway import MAINTAINER, FakePull
from pipeline_helpers import (
    BOTH,
    HELLO,
    HI,
    Harness,
    advance_main,
    assert_no_forbidden_writes,
    git,
    only_task,
    open_pr,
    parents_of,
    refine_json,
    review_json,
    selection_json,
    worker_json,
    workspace_editor,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import RefineState, RunState, TaskKind, TaskState
from skeleton_crew.github.gateway import BranchRule, CiState, CiStatus, RulesUnavailable
from skeleton_crew.pipeline.run_controller import RunController
from skeleton_crew.runtime.containers import ContainerHandle
from skeleton_crew.store import runs_store
from skeleton_crew.web.ports import RunControl

GREETING = {"app.py": "print('hi')\n"}


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    return Harness(tmp_path, sessionmaker)


def test_run_controller_satisfies_run_control(harness: Harness) -> None:
    control: RunControl = harness.controller
    assert control.is_running is False


async def test_selector_too_large_escalates_without_worker(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Rewrite everything")
    harness.backend.script(Role.SELECTOR, selection_json([], too_large=[2]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == ["comment", "change_labels"]
    assert harness.writes("change_labels")[0].args["issue"] == 2
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    selector = harness.runtime.started[0]
    assert selector.mounts[0].read_only
    assert not (selector.mounts[0].host_path / ".git").exists()
    assert_no_forbidden_writes(harness)


async def test_selector_rejected_issue_escalates_with_reason(harness: Harness) -> None:
    harness.add_ready_issue(3, "Leak env")
    harness.backend.script(Role.SELECTOR, selection_json([], rejected=[3]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == ["comment", "change_labels"]
    assert "rejected the issue: unsafe" in str(harness.writes("comment")[0].args["body"])
    assert harness.writes("change_labels")[0].args["issue"] == 3
    assert_no_forbidden_writes(harness)


async def test_selector_decision_escalates_with_question_and_options(harness: Harness) -> None:
    harness.add_ready_issue(4, "Default reply-to")
    harness.backend.script(Role.SELECTOR, selection_json([], needs_decision=[4]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == ["comment", "change_labels"]
    body = str(harness.writes("comment")[0].args["body"])
    assert "needs a decision: Fallback address is not specified." in body
    assert "Question: Which address should replies go to?" in body
    assert "- owner account\n- info address from .env" in body
    assert harness.writes("change_labels")[0].args["issue"] == 4
    assert_no_forbidden_writes(harness)


async def test_selector_leaves_unlisted_candidates_untouched(harness: Harness) -> None:
    harness.add_ready_issue(5)
    harness.add_ready_issue(6)
    harness.backend.script(Role.SELECTOR, selection_json([]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == []
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED


async def test_selector_with_unknown_issue_fails_closed(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.backend.script(Role.SELECTOR, selection_json([1, 99]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []
    run = await harness.run_row(run_id)
    assert run.state is RunState.FAILED
    assert "unknown issues" in str(run.note)


async def test_untrusted_labeler_and_hitl_and_open_pr_issues_are_ignored(
    harness: Harness,
) -> None:
    harness.gateway.add_issue(1, "Evil", labels=["skeleton"], labeled_by="stranger")
    harness.gateway.add_issue(2, "Waiting", labels=["skeleton", "skeleton HITL"])
    harness.gateway.add_issue(3, "Busy", labels=["skeleton"], labeled_by=MAINTAINER)
    harness.gateway.pulls.append(FakePull(10, "skeleton/3-busy", "a"))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.backend.requests == []
    assert harness.gateway.writes == []
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED


async def test_refine_flow_comments_and_swaps_labels(harness: Harness) -> None:
    harness.gateway.add_issue(5, "Idea", body="Maybe greet?", labels=["skeleton refine"])
    harness.backend.script(Role.REFINER, refine_json())

    run_id = await harness.run()

    [refine] = await harness.refines(run_id)
    assert refine.state is RefineState.DONE
    assert harness.actions() == ["comment", "change_labels"]
    [comment] = harness.writes("comment")
    assert str(comment.args["body"]).startswith("## Refined spec\n\nGoal: greet.")
    assert "Which language?" in str(comment.args["body"])
    assert harness.gateway.issues[5].labels == {"skeleton HITL"}
    assert harness.runtime.started[0].mounts[0].read_only
    assert_no_forbidden_writes(harness)


async def test_invalid_refine_output_fails_without_writes(harness: Harness) -> None:
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.backend.script(Role.REFINER, '{"spec_markdown": ""}')

    run_id = await harness.run()

    [refine] = await harness.refines(run_id)
    assert refine.state is RefineState.FAILED
    assert harness.gateway.writes == []


async def test_dry_run_makes_no_github_writes(harness: Harness) -> None:
    harness.settings = harness.settings.model_copy(update={"dry_run": True})
    harness.add_ready_issue(1)
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json(new_issues=[{"title": "T", "body": "B"}]))
    harness.backend.script(Role.REVIEWER, review_json())
    harness.backend.script(Role.REFINER, refine_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.writes == []
    assert harness.gateway.pushes == []
    assert git(harness.remote, "for-each-ref").strip() == b""
    dry = [k for k, o in await harness.audit_kinds() if o is AuditOutcome.DRY_RUN]
    assert len(dry) == 6
    assert (await harness.run_row(run_id)).dry_run is True


async def test_preflight_failure_stops_the_run(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.runtime.health_problems = ["docker runtime sysbox-runc is not installed"]
    harness.backend.problems = ["model token missing"]
    harness.gateway.ruleset_bypasses[1] = "always"

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.PREFLIGHT_FAILED
    assert "sysbox-runc" in str(run.note)
    assert "model token missing" in str(run.note)
    assert "bypass" in str(run.note)
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
    assert (AuditKind.PREFLIGHT_FAILED, AuditOutcome.BLOCKED) in await harness.audit_kinds()


@pytest.mark.parametrize(
    "update",
    [
        {"agents": {"model": "model-removed-in-a-release"}},
        {"agents": {"effort": "ultra"}},
        {"agents": {"backend": "gone_backend"}},
        {"backend_options": {"claude_code": {"auth_mode": "root"}}},
        {"backend_options": {"claude_code": {"unknown_option": "x"}}},
    ],
)
async def test_invalid_agent_settings_fail_preflight(
    harness: Harness, update: dict[str, dict[str, object]]
) -> None:
    harness.add_ready_issue(1)
    settings = harness.settings
    if "agents" in update:
        worker = settings.agents[Role.WORKER].model_copy(update=update["agents"])
        settings = settings.model_copy(update={"agents": {**settings.agents, Role.WORKER: worker}})
    else:
        settings = settings.model_copy(update=update)
    harness.settings = settings

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.PREFLIGHT_FAILED
    assert "agents.worker" in str(run.note) or "backend_options.claude_code" in str(run.note)
    assert harness.backend.requests == []
    assert harness.backend_builds == []
    assert harness.runtime.started == []
    assert harness.gateway.writes == []


async def test_valid_settings_build_each_configured_backend_once(harness: Harness) -> None:
    await harness.run()

    assert harness.backend_builds == ["claude_code"]


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


async def test_egress_allowlist_includes_backend_domains(harness: Harness) -> None:
    await harness.run()

    allowlist = harness.config.egress_allowlist_path.read_text().split()
    assert "model.example.com" in allowlist
    assert "pypi.org" in allowlist
    assert any(c.argv[0] == "squid" for c in harness.runtime.execs)


async def test_kill_switch_refuses_to_start(harness: Harness) -> None:
    harness.settings = harness.settings.model_copy(update={"kill_switch": True})
    await harness.save_settings()

    assert await harness.controller.start("manual") is None
    assert not harness.controller.is_running


async def test_quota_counts_tasks_from_earlier_runs_today(harness: Harness) -> None:
    harness.limits(issues_per_day=1)
    harness.add_ready_issue(1)
    async with harness.sessionmaker() as session:
        run = await runs_store.create_run(session, "manual", dry_run=False)
        await runs_store.add_worker_task(session, run.id, 7, "Earlier")
        await runs_store.finish_run(session, run.id, RunState.COMPLETED)
        await session.commit()

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.backend.requests == []


async def test_kill_aborts_running_worker_and_removes_containers(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(GREETING)]
    harness.gateway.ci_queue = [CiStatus(CiState.PENDING, [])]
    harness.sleep_hook = asyncio.Event()
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    await harness.save_settings()

    run_id = await harness.controller.start("manual")
    assert run_id is not None
    assert await harness.controller.start("manual") is None
    await asyncio.wait_for(harness.sleeping.wait(), 10)
    assert harness.runtime.running
    await harness.controller.kill()

    assert not harness.controller.is_running
    assert not harness.runtime.running
    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ABORTED
    assert (await harness.run_row(run_id)).state is RunState.ABORTED
    assert harness.actions() == ["push_branch", "open_pr"]
    assert AuditKind.KILL_SWITCH not in {k for k, _ in await harness.audit_kinds()}


async def test_window_end_stops_new_workers(harness: Harness) -> None:
    harness.settings = harness.settings.model_copy(
        update={"schedule": harness.settings.schedule.model_copy(update={"window_hours": 0.1})}
    )
    harness.limits(max_parallel_workers=1, ci_poll_seconds=600)
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Second")
    harness.patches = [harness.patch(GREETING)]
    harness.gateway.ci_queue = [CiStatus(CiState.PENDING, []), CiStatus(CiState.SUCCESS, [])]
    harness.backend.script(Role.SELECTOR, selection_json([1, 2]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.issue_number, task.state) == (1, TaskState.DONE)
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED


async def test_parallel_workers_and_refines_complete(harness: Harness) -> None:
    harness.limits(max_parallel_workers=2)
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Second")
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.patches = [harness.patch(GREETING)]
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
    async with harness.sessionmaker() as session:
        live = await runs_store.create_run(session, "schedule", dry_run=False)
        task = await runs_store.add_worker_task(session, live.id, 1, "Add greeting")
        await runs_store.update_worker_task(session, task.id, state=TaskState.AWAITING_CI)
        refine = await runs_store.add_refine_task(session, live.id, 5)
        dry = await runs_store.create_run(session, "manual", dry_run=True)
        await runs_store.add_worker_task(session, dry.id, 2, "Dry")
        await session.commit()
    harness.runtime.running["leftover"] = ContainerHandle("leftover", "skeleton-crew-x")
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


def _require_tests_check(harness: Harness) -> None:
    rule = BranchRule(
        "required_status_checks", 2, {"required_status_checks": [{"context": "tests"}]}
    )
    harness.gateway.rules.append(rule)


async def test_ci_waits_for_the_checks_the_ruleset_requires(harness: Harness) -> None:
    _require_tests_check(harness)
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.ci_required
    assert set(harness.gateway.ci_required) == {frozenset({"tests"})}


async def test_repo_without_rulesets_waits_for_no_required_checks(harness: Harness) -> None:
    harness.repo(allow_unprotected_base_branch=True)
    harness.gateway.failures["branch_rules"] = RulesUnavailable()
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    assert set(harness.gateway.ci_required) == {frozenset()}


async def test_unreadable_required_checks_fail_the_run_closed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.add_ready_issue(1)
    real = harness.gateway.branch_rules
    calls = 0

    async def flaky(branch: str) -> list[BranchRule]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise TimeoutError
        return await real(branch)

    monkeypatch.setattr(harness.gateway, "branch_rules", flaky)

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.PREFLIGHT_FAILED
    assert "required checks could not be read" in str(run.note)
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
