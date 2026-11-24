import asyncio

import pytest
from git_helpers import git
from pipeline_helpers import (
    BLOCKER,
    GREETING,
    Harness,
    assert_no_forbidden_writes,
    only_task,
    review_json,
    worker_json,
)

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.ci_policy import CiState, CiStatus, FailedJob, ReviewCheck
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskState

pytestmark = pytest.mark.usefixtures("selected_issue")


def escalation_writes(issue: int = 1) -> list[tuple[str, dict[str, object]]]:
    return [("change_labels", {"issue": issue, "add": ["skeleton HITL"], "remove": ["skeleton"]})]


async def test_ci_failure_resumes_worker_with_logs_then_passes(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING), harness.patch({"app.py": "print('fixed')\n"})]
    harness.gateway.ci_default = [
        CiStatus(CiState.PENDING, []),
        CiStatus(CiState.FAILURE, [FailedJob("tests", 7, "https://ci/7")]),
        CiStatus(CiState.SUCCESS, []),
    ]
    harness.gateway.job_logs[7] = "AssertionError: boom"
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.ci_rounds) == (TaskState.DONE, 1)
    assert harness.actions() == [
        "push_branch",
        "set_review_check",
        "open_pr",
        "push_branch",
        "set_review_check",
        "set_review_check",
    ]
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

    task = await only_task(harness, run_id)
    assert (task.state, task.review_rounds) == (TaskState.DONE, 1)
    checks = [(w.action, w.args.get("state")) for w in harness.gateway.writes]
    assert checks == [
        ("push_branch", None),
        ("set_review_check", ReviewCheck.PENDING),
        ("open_pr", None),
        ("set_review_check", ReviewCheck.CHANGES_REQUESTED),
        ("push_branch", None),
        ("set_review_check", ReviewCheck.PENDING),
        ("set_review_check", ReviewCheck.PASSED),
    ]
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert "Handle empty input." in follow_up.prompt
    assert follow_up.resume_session_id == "worker-session"


async def test_missing_doc_update_alone_requests_changes(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING), harness.patch({"README.md": "Greets.\n"})]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    missing: dict[str, object] = {"path": "README.md", "reason": "Does not mention the greeting."}
    harness.backend.script(
        Role.REVIEWER, review_json("changes_requested", missing_docs=[missing]), review_json()
    )

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.review_rounds) == (TaskState.DONE, 1)
    _, first_check, _, _ = harness.writes("set_review_check")
    assert first_check.args["state"] is ReviewCheck.CHANGES_REQUESTED
    assert "- [docs] README.md: Does not mention the greeting." in str(first_check.args["summary"])
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert "- [docs] README.md: Does not mention the greeting." in follow_up.prompt


async def test_ci_that_never_finishes_leaves_the_review_check_pending(harness: Harness) -> None:
    harness.set("limits", ci_timeout_minutes=5)
    harness.patches = [harness.patch(GREETING)]
    harness.gateway.ci_default = [CiStatus(CiState.PENDING, [])]
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "CI did not finish in time" in str(task.note)
    assert harness.backend.prompts(Role.REVIEWER) == []
    checks = [(w.args["sha"], w.args["state"]) for w in harness.writes("set_review_check")]
    assert checks == [(task.head_sha, ReviewCheck.PENDING)]


async def test_review_rounds_exhausted_escalates(harness: Harness) -> None:
    harness.set("limits", max_review_rounds=1)
    harness.patches = [harness.patch(GREETING), harness.patch({"app.py": "print(1)\n"})]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    changes = review_json("changes_requested", [BLOCKER])
    harness.backend.script(Role.REVIEWER, changes, changes)

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert task.note == "the reviewer still requests changes after 1 fix rounds"
    assert task.review_rounds == 1
    assert harness.actions()[-2:] == ["comment", "change_labels"]
    labels = harness.writes("change_labels")
    assert [("change_labels", dict(w.args)) for w in labels] == escalation_writes()
    assert harness.gateway.issues[1].labels == {"skeleton HITL"}
    assert (AuditKind.TASK_ESCALATED, AuditOutcome.INFO) in await harness.audit_kinds()
    assert_no_forbidden_writes(harness)


async def test_ci_rounds_exhausted_escalates(harness: Harness) -> None:
    harness.set("limits", max_ci_rounds=0)
    harness.patches = [harness.patch(GREETING)]
    harness.gateway.ci_default = [CiStatus(CiState.FAILURE, [])]
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert (task.note, task.ci_rounds) == ("CI still fails after 0 fix rounds", 0)
    assert harness.actions() == [
        "push_branch",
        "set_review_check",
        "open_pr",
        "comment",
        "change_labels",
    ]


async def test_check_rounds_do_not_use_up_ci_rounds(harness: Harness) -> None:
    harness.set("limits", max_ci_rounds=0, max_check_rounds=1)
    harness.set("repo", check_command="make check")
    harness.repo_commands((1, b"FAILED"), (0, b"ok"))
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.check_rounds, task.ci_rounds) == (TaskState.DONE, 1, 0)


async def test_policy_rounds_do_not_use_up_review_rounds(harness: Harness) -> None:
    harness.set("limits", max_policy_rounds=1, max_review_rounds=1)
    workflow = {".github/workflows/ci.yml": "on: push\n"}
    harness.patches = [
        harness.patch(workflow),
        harness.patch(GREETING),
        harness.patch({"app.py": "print(1)\n"}),
    ]
    harness.backend.script(Role.WORKER, worker_json(), worker_json(), worker_json())
    changes = review_json("changes_requested", [BLOCKER])
    harness.backend.script(Role.REVIEWER, changes, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds, task.review_rounds) == (TaskState.DONE, 1, 1)


async def test_check_rounds_exhausted_escalates_without_push(harness: Harness) -> None:
    harness.set("limits", max_check_rounds=0, max_ci_rounds=5)
    harness.set("repo", check_command="make check")
    harness.repo_commands((1, b"FAILED"))
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert task.note == "the check command still fails after 0 fix rounds"
    assert task.check_rounds == 0
    assert harness.actions() == ["comment", "change_labels"]


async def test_worker_timeout_escalates(harness: Harness) -> None:
    harness.set("limits", worker_timeout_hours=0.0002)
    harness.patches = [harness.patch(GREETING)]
    harness.gateway.ci_default = [CiStatus(CiState.PENDING, [])]
    harness.sleep_hook = asyncio.Event()
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "timed out or the run window ended while waiting for CI" in str(task.note)
    assert harness.actions() == [
        "push_branch",
        "set_review_check",
        "open_pr",
        "comment",
        "change_labels",
    ]
    assert not harness.runtime.running
