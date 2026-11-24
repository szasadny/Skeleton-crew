import pytest
from git_helpers import git
from pipeline_helpers import (
    BRANCH,
    GREETING,
    Harness,
    assert_no_forbidden_writes,
    is_repo_command,
    only_task,
    review_json,
    worker_json,
)

from skeleton_crew.domain.ci_policy import ReviewCheck
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.pipeline.texts import REVIEW_PENDING_SUMMARY

pytestmark = pytest.mark.usefixtures("selected_issue")


async def test_happy_path_opens_pr_and_sets_green_review_check(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(summary="Greets. Fixes #9 for @octocat"))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr", "set_review_check"]
    pending, passed = harness.writes("set_review_check")
    assert pending.args == {
        "sha": task.head_sha,
        "state": ReviewCheck.PENDING,
        "summary": REVIEW_PENDING_SUMMARY,
    }
    assert (passed.args["sha"], passed.args["state"]) == (task.head_sha, ReviewCheck.PASSED)
    [push] = harness.writes("push_branch")
    assert push.args == {"branch": BRANCH, "sha": task.head_sha}
    [pr] = harness.writes("open_pr")
    assert pr.args["title"] == "[skeleton] Add greeting"
    body = str(pr.args["body"])
    assert body.startswith("Closes #1\n\n")
    assert "@octocat" not in body
    assert "Fixes #9" not in body
    assert harness.gateway.review_checks[str(task.head_sha)][0] is ReviewCheck.PASSED
    commit = git(harness.remote, "cat-file", "-p", str(task.head_sha)).decode()
    assert f"parent {task.base_sha}" in commit
    assert "author Skeleton Crew" in commit
    assert git(harness.remote, "show", f"{task.head_sha}:app.py").decode() == GREETING["app.py"]
    assert task.pr_number is not None
    assert not any(is_repo_command(call.argv) for call in harness.runtime.execs)
    assert_no_forbidden_writes(harness)


async def test_worker_scope_too_large_escalates_without_push(harness: Harness) -> None:
    harness.backend.script(Role.WORKER, worker_json("scope_too_large", note="Needs a new service."))

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert harness.actions() == ["comment", "change_labels"]
    [comment] = harness.writes("comment")
    assert "Needs a new service." in str(comment.args["body"])
    assert not harness.runtime.running
    assert_no_forbidden_writes(harness)


async def test_worker_already_fixed_escalates_without_push(harness: Harness) -> None:
    harness.backend.script(Role.WORKER, worker_json("already_fixed", note="Fixed by #932."))

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.ESCALATED, 0)
    assert harness.actions() == ["comment", "change_labels"]
    [comment] = harness.writes("comment")
    assert "Fixed by #932." in str(comment.args["body"])
    assert harness.gateway.issues[1].labels == {"skeleton HITL"}
    assert not harness.runtime.running
    assert_no_forbidden_writes(harness)
