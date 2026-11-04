import asyncio
import json
from pathlib import Path

import pytest
from fake_agent import stopped_outcome
from fake_runtime import ExecCall
from pipeline_helpers import (
    BLOCKER,
    Harness,
    assert_no_forbidden_writes,
    command_result,
    git,
    is_repo_command,
    review_json,
    selection_json,
    worker_json,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.agents.base import failed_outcome
from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.github.gateway import CiState, CiStatus, FailedJob
from skeleton_crew.pipeline.sessions import AGENT_USER, INBOX, WORKSPACE
from skeleton_crew.runtime.containers import ExecResult

BRANCH = "skeleton/1-add-greeting"
GREETING = {"app.py": "def main() -> None:\n    print('hi')\n"}
COMMENTED = {"app.py": "# Licensed under X.\n# See LICENSE.\n# All rights reserved.\nprint('hi')\n"}
LICENSE_OBJECTION = {"path": "app.py", "line": 1, "reason": "License header the repo requires."}


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    h = Harness(tmp_path, sessionmaker)
    h.add_ready_issue()
    h.backend.script(Role.SELECTOR, selection_json([1]))
    return h


def escalation_writes(issue: int = 1) -> list[tuple[str, dict[str, object]]]:
    return [("change_labels", {"issue": issue, "add": ["skeleton HITL"], "remove": ["skeleton"]})]


async def test_happy_path_opens_pr_and_sets_green_review_check(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(summary="Greets. Fixes #9 for @octocat"))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.DONE
    assert harness.actions() == ["push_branch", "open_pr", "set_review_check"]
    [push] = harness.writes("push_branch")
    assert push.args == {"branch": BRANCH, "sha": task.head_sha}
    [pr] = harness.writes("open_pr")
    assert pr.args["title"] == "[skeleton] Add greeting"
    body = str(pr.args["body"])
    assert body.startswith("Closes #1\n\n")
    assert "@octocat" not in body
    assert "Fixes #9" not in body
    assert harness.gateway.review_checks[str(task.head_sha)][0] is True
    commit = git(harness.remote, "cat-file", "-p", str(task.head_sha)).decode()
    assert f"parent {task.base_sha}" in commit
    assert "author Skeleton Crew" in commit
    assert git(harness.remote, "show", f"{task.head_sha}:app.py").decode() == GREETING["app.py"]
    assert task.pr_number is not None
    assert not any(is_repo_command(call.argv) for call in harness.runtime.execs)
    assert_no_forbidden_writes(harness)


async def test_reviewer_runs_in_separate_readonly_container_with_the_diff(
    harness: Harness,
) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    roles = [spec.labels["skeleton-crew.role"] for spec in harness.runtime.started]
    assert roles == ["selector", "worker", "reviewer"]
    worker, reviewer = harness.runtime.started[1], harness.runtime.started[2]
    assert [(m.container_path, m.read_only) for m in worker.mounts] == [
        (WORKSPACE, False),
        (INBOX, True),
    ]
    assert [(m.container_path, m.read_only) for m in reviewer.mounts] == [
        (WORKSPACE, True),
        (INBOX, True),
    ]
    assert worker.mounts[0].host_path != reviewer.mounts[0].host_path
    assert "HTTPS_PROXY" in worker.env
    assert not harness.runtime.running
    assert harness.backend.prompts(Role.REVIEWER)[0].resume_session_id is None
    [task] = await harness.tasks(run_id)
    root = harness.config.tasks_dir / f"worker-{task.id}"
    assert sorted(p.name for p in root.iterdir()) == ["transcripts"]
    transcripts = sorted(p.name for p in (root / "transcripts").iterdir())
    assert transcripts == ["reviewer-1.jsonl", "worker-1.jsonl"]
    patch_calls = [c for c in harness.runtime.execs if c.argv[0] == "sh"]
    assert patch_calls[0].env == {}
    assert patch_calls[0].argv[-1] == task.base_sha


async def test_ci_failure_resumes_worker_with_logs_then_passes(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING), harness.patch({"app.py": "print('fixed')\n"})]
    harness.gateway.ci_queue = [
        CiStatus(CiState.PENDING, []),
        CiStatus(CiState.FAILURE, [FailedJob("tests", 7, "https://ci/7")]),
        CiStatus(CiState.SUCCESS, []),
    ]
    harness.gateway.job_logs[7] = "AssertionError: boom"
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.ci_rounds) == (TaskState.DONE, 1)
    assert harness.actions() == ["push_branch", "open_pr", "push_branch", "set_review_check"]
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert follow_up.resume_session_id == "worker-session"
    assert "AssertionError: boom" in follow_up.prompt
    first, second = (w.args["sha"] for w in harness.writes("push_branch"))
    assert git(harness.remote, "rev-parse", f"{second}^").decode().strip() == first
    assert harness.sleeps == [harness.settings.limits.ci_poll_seconds]
    assert_no_forbidden_writes(harness)


async def test_review_changes_then_approve(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING), harness.patch({"app.py": "print(1)\n"})]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(
        Role.REVIEWER, review_json("changes_requested", [BLOCKER]), review_json()
    )

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.review_rounds) == (TaskState.DONE, 1)
    checks = [(w.action, w.args.get("success")) for w in harness.gateway.writes]
    assert checks == [
        ("push_branch", None),
        ("open_pr", None),
        ("set_review_check", False),
        ("push_branch", None),
        ("set_review_check", True),
    ]
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert "Handle empty input." in follow_up.prompt
    assert follow_up.resume_session_id == "worker-session"


async def test_review_rounds_exhausted_escalates(harness: Harness) -> None:
    harness.limits(max_review_rounds=1)
    harness.patches = [harness.patch(GREETING), harness.patch({"app.py": "print(1)\n"})]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    changes = review_json("changes_requested", [BLOCKER])
    harness.backend.script(Role.REVIEWER, changes, changes)

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "1 policy or review rounds" in str(task.escalation_reason)
    assert harness.actions()[-2:] == ["comment", "change_labels"]
    labels = harness.writes("change_labels")
    assert [("change_labels", dict(w.args)) for w in labels] == escalation_writes()
    assert harness.gateway.issues[1].labels == {"skeleton HITL"}
    assert (AuditKind.TASK_ESCALATED, AuditOutcome.INFO) in await harness.audit_kinds()
    assert_no_forbidden_writes(harness)


async def test_ci_rounds_exhausted_escalates(harness: Harness) -> None:
    harness.limits(max_ci_rounds=0)
    harness.patches = [harness.patch(GREETING)]
    harness.gateway.ci_queue = [CiStatus(CiState.FAILURE, [])]
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert harness.actions() == ["push_branch", "open_pr", "comment", "change_labels"]


async def test_worker_scope_too_large_escalates_without_push(harness: Harness) -> None:
    harness.backend.script(Role.WORKER, worker_json("scope_too_large", note="Needs a new service."))

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert harness.actions() == ["comment", "change_labels"]
    [comment] = harness.writes("comment")
    assert "Needs a new service." in str(comment.args["body"])
    assert not harness.runtime.running
    assert_no_forbidden_writes(harness)


async def test_protected_path_edit_gets_follow_up_then_escalates(harness: Harness) -> None:
    harness.limits(max_review_rounds=1)
    workflow = {".github/workflows/ci.yml": "on: push\n"}
    harness.patches = [harness.patch(workflow)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.policy_rounds) == (TaskState.ESCALATED, 1)
    assert harness.actions() == ["comment", "change_labels"]
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert ".github/workflows/ci.yml: protected path" in follow_up.prompt
    assert git(harness.remote, "for-each-ref").strip() == b""
    assert_no_forbidden_writes(harness)


async def test_secret_in_patch_is_a_policy_violation(harness: Harness) -> None:
    harness.limits(max_review_rounds=0)
    harness.patches = [harness.patch(GREETING)]
    harness.secret_findings = ["generic-api-key in app.py"]
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "push_branch" not in harness.actions()


async def test_new_issues_are_capped_and_created_with_hitl_only(harness: Harness) -> None:
    harness.limits(max_new_issues_per_worker=2)
    harness.patches = [harness.patch(GREETING)]
    found = [{"title": f"Bug {n} @octocat", "body": "Closes #1"} for n in range(3)]
    harness.backend.script(Role.WORKER, worker_json(new_issues=found))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.DONE
    created = harness.writes("create_issue")
    assert len(created) == 2
    for write in created:
        assert "@octocat" not in str(write.args["title"])
        assert str(write.args["body"]).endswith("Found while working on #1.")
        assert "Closes #1" not in str(write.args["body"])
        assert harness.gateway.issues[int(str(write.args["number"]))].labels == {"skeleton HITL"}
    assert_no_forbidden_writes(harness)


@pytest.mark.parametrize(
    "output",
    ["{not json", json.dumps({"status": "done"}), worker_json(pr_title="x" * 80)],
)
async def test_invalid_worker_output_escalates(harness: Harness, output: str) -> None:
    harness.backend.script(Role.WORKER, output)

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "invalid WorkerResult" in str(task.escalation_reason)
    assert harness.actions() == ["comment", "change_labels"]


async def test_failed_session_escalates(harness: Harness) -> None:
    harness.backend.script(Role.WORKER, failed_outcome("session ended with error_max_turns"))

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "error_max_turns" in str(task.escalation_reason)
    assert len(harness.backend.prompts(Role.WORKER)) == 1


async def test_hook_stop_is_continued_and_the_worker_finishes(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, stopped_outcome(), stopped_outcome(), worker_json())
    harness.backend.script(Role.REVIEWER, stopped_outcome(session_id="review"), review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.DONE
    assert harness.actions() == ["push_branch", "open_pr", "set_review_check"]
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
    harness.limits(max_continues=2)
    harness.backend.script(Role.WORKER, *(stopped_outcome() for _ in range(4)))

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "stopped early: hook_stopped" in str(task.escalation_reason)
    assert len(harness.backend.prompts(Role.WORKER)) == 3
    assert harness.actions() == ["comment", "change_labels"]


@pytest.mark.parametrize(("max_continues", "session_id"), [(0, "worker-session"), (5, None)])
async def test_early_stop_is_not_continued_without_budget_or_session(
    harness: Harness, max_continues: int, session_id: str | None
) -> None:
    harness.limits(max_continues=max_continues)
    harness.backend.script(Role.WORKER, stopped_outcome(session_id=session_id), worker_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert len(harness.backend.prompts(Role.WORKER)) == 1


async def test_worker_timeout_escalates(harness: Harness) -> None:
    harness.limits(worker_timeout_hours=0.0002)
    harness.patches = [harness.patch(GREETING)]
    harness.gateway.ci_queue = [CiStatus(CiState.PENDING, [])]
    harness.sleep_hook = asyncio.Event()
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "timed out" in str(task.escalation_reason)
    assert harness.actions() == ["push_branch", "open_pr", "comment", "change_labels"]
    assert not harness.runtime.running


def _commands(harness: Harness, *results: tuple[int, bytes]) -> list[ExecCall]:
    pending = list(results)
    calls: list[ExecCall] = []

    def respond(call: ExecCall) -> ExecResult:
        calls.append(call)
        code, output = pending.pop(0) if len(pending) > 1 else pending[0]
        return command_result(code, output)

    harness.runtime.script(is_repo_command, respond)
    return calls


async def test_long_comment_block_goes_back_to_the_worker_before_any_push(
    harness: Harness,
) -> None:
    harness.patches = [harness.patch(COMMENTED), harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    assert harness.actions() == ["push_branch", "open_pr", "set_review_check"]
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert "app.py:1: comment block of 3 lines, limit is 2" in follow_up.prompt
    [push] = harness.writes("push_branch")
    assert git(harness.remote, "show", f"{push.args['sha']}:app.py").decode() == GREETING["app.py"]


async def test_contested_comment_block_is_judged_by_the_reviewer(harness: Harness) -> None:
    harness.patches = [harness.patch(COMMENTED)]
    harness.backend.script(
        Role.WORKER, worker_json(), worker_json(comment_objections=[LICENSE_OBJECTION])
    )
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    [review] = harness.backend.prompts(Role.REVIEWER)
    assert "app.py:1 (3 comment lines), worker reason: License header" in review.prompt
    assert harness.actions() == ["push_branch", "open_pr", "set_review_check"]


async def test_comment_block_contested_up_front_costs_no_policy_round(harness: Harness) -> None:
    harness.patches = [harness.patch(COMMENTED)]
    harness.backend.script(Role.WORKER, worker_json(comment_objections=[LICENSE_OBJECTION]))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 0)


async def test_rejected_contest_goes_back_through_review(harness: Harness) -> None:
    harness.patches = [harness.patch(COMMENTED), harness.patch(GREETING)]
    harness.backend.script(
        Role.WORKER, worker_json(comment_objections=[LICENSE_OBJECTION]), worker_json()
    )
    unjustified = {"severity": "major", "path": "app.py", "line": 1, "message": "Not needed."}
    harness.backend.script(
        Role.REVIEWER, review_json("changes_requested", [unjustified]), review_json()
    )

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.review_rounds, task.policy_rounds) == (TaskState.DONE, 1, 0)
    assert "Not needed." in harness.backend.prompts(Role.WORKER)[1].prompt


async def test_objection_on_another_line_does_not_lift_the_flag(harness: Harness) -> None:
    harness.limits(max_review_rounds=1)
    harness.patches = [harness.patch(COMMENTED)]
    wrong_line = {**LICENSE_OBJECTION, "line": 2}
    harness.backend.script(
        Role.WORKER,
        worker_json(comment_objections=[wrong_line]),
        worker_json(comment_objections=[wrong_line]),
    )

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "push_branch" not in harness.actions()
    assert harness.backend.prompts(Role.REVIEWER) == []


async def test_setup_command_runs_before_the_worker_without_credentials(
    harness: Harness,
) -> None:
    harness.repo(setup_command="make deps")
    calls = _commands(harness, (0, b"installed"))
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
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
    harness.repo(setup_command="make deps")
    _commands(harness, (2, b"no such target"))
    harness.patches = [harness.patch(GREETING)]

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "the setup command failed (exit 2)" in str(task.escalation_reason)
    assert harness.backend.prompts(Role.WORKER) == []
    [comment] = harness.writes("comment")
    assert "no such target" not in str(comment.args["body"])


async def test_failed_check_goes_back_to_the_worker_before_any_push(harness: Harness) -> None:
    harness.repo(check_command="make check")
    calls = _commands(harness, (1, b"FAILED test_app"), (0, b"ok"))
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.state, task.ci_rounds, task.policy_rounds) == (TaskState.DONE, 1, 0)
    assert len(calls) == 2
    assert all(c.env == {} for c in calls)
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert follow_up.resume_session_id == "worker-session"
    assert "repository checks failed" in follow_up.prompt
    assert "`make check` failed (exit 1)" in follow_up.prompt
    assert "FAILED test_app" in follow_up.prompt
    assert harness.actions() == ["push_branch", "open_pr", "set_review_check"]


async def test_check_rounds_exhausted_escalates_without_push(harness: Harness) -> None:
    harness.limits(max_ci_rounds=0)
    harness.repo(check_command="make check")
    _commands(harness, (1, b"FAILED"))
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert task.state is TaskState.ESCALATED
    assert "check command still fails after 0 fix rounds" in str(task.escalation_reason)
    assert harness.actions() == ["comment", "change_labels"]
