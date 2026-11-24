import pytest
from pipeline_helpers import (
    GREETING,
    Harness,
    assert_no_forbidden_writes,
    only_task,
    review_json,
    worker_json,
)

from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.runtime.containers import AGENT_USER, WORKSPACE

pytestmark = pytest.mark.usefixtures("selected_issue")


async def test_setup_command_runs_before_the_worker_without_credentials(
    harness: Harness,
) -> None:
    harness.set("repo", setup_command="make deps")
    calls = harness.repo_commands((0, b"installed"))
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    [setup] = calls
    assert "make deps" in setup.argv[2]
    assert (setup.env, setup.user, setup.cwd) == ({}, AGENT_USER, WORKSPACE)
    worker_execs = [c for c in harness.runtime.execs if c.handle.id == setup.handle.id]
    assert worker_execs[0] is setup
    assert worker_execs[1].argv[:2] == ("fake-agent", "worker")
    assert "already ran `make deps`" in harness.backend.prompts(Role.WORKER)[0].prompt
    assert_no_forbidden_writes(harness)


async def test_failed_setup_command_escalates_without_a_worker_session(
    harness: Harness,
) -> None:
    harness.set("repo", setup_command="make deps")
    harness.repo_commands((2, b"no such target"))
    harness.patches = [harness.patch(GREETING)]

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "the setup command failed (exit 2)" in str(task.note)
    assert harness.backend.prompts(Role.WORKER) == []
    [comment] = harness.writes("comment")
    assert "no such target" not in str(comment.args["body"])


async def test_failed_check_goes_back_to_the_worker_before_any_push(harness: Harness) -> None:
    harness.set("repo", check_command="make check")
    calls = harness.repo_commands((1, b"FAILED test_app"), (0, b"ok"))
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.check_rounds, task.ci_rounds) == (TaskState.DONE, 1, 0)
    assert len(calls) == 2
    assert all(c.env == {} for c in calls)
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert follow_up.resume_session_id == "worker-session"
    assert "repository checks failed" in follow_up.prompt
    assert "`make check` failed (exit 1)" in follow_up.prompt
    assert "FAILED test_app" in follow_up.prompt
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr", "set_review_check"]
