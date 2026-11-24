import pytest
from git_helpers import git
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
from skeleton_crew.runtime.controller_git import AGENT_PATCH_ARGV, MAX_PATCH_BYTES

pytestmark = pytest.mark.usefixtures("selected_issue")


COMMENTED = {"app.py": "# Licensed under X.\n# See LICENSE.\n# All rights reserved.\nprint('hi')\n"}


LICENSE_OBJECTION = {
    "check": "comment_block",
    "path": "app.py",
    "line": 1,
    "reason": "License header the repo requires.",
}


async def test_protected_path_edit_gets_follow_up_then_escalates(harness: Harness) -> None:
    harness.set("limits", max_policy_rounds=1)
    workflow = {".github/workflows/ci.yml": "on: push\n"}
    harness.patches = [harness.patch(workflow)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.ESCALATED, 1)
    assert task.note == "the change still breaks policy after 1 fix rounds"
    assert harness.actions() == ["comment", "change_labels"]
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert ".github/workflows/ci.yml: protected path" in follow_up.prompt
    assert git(harness.remote, "for-each-ref").strip() == b""
    assert_no_forbidden_writes(harness)


async def test_secret_in_patch_is_a_policy_violation(harness: Harness) -> None:
    harness.set("limits", max_policy_rounds=0)
    harness.patches = [harness.patch(GREETING)]
    harness.secret_findings = ["generic-api-key in app.py"]
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "push_branch" not in harness.actions()


async def test_oversized_patch_is_read_capped_and_sent_back(harness: Harness) -> None:
    harness.set("limits", max_policy_rounds=1)
    harness.patches = [b"x" * (MAX_PATCH_BYTES + 1024), harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    reads = [
        c for c in harness.runtime.execs if c.argv[: len(AGENT_PATCH_ARGV)] == AGENT_PATCH_ARGV
    ]
    assert [c.max_stdout_bytes for c in reads] == [MAX_PATCH_BYTES + 1] * 2
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert f"patch larger than {MAX_PATCH_BYTES} bytes" in follow_up.prompt
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr", "set_review_check"]


async def test_new_issues_are_capped_and_created_with_hitl_only(harness: Harness) -> None:
    harness.set("limits", max_new_issues_per_worker=2)
    harness.patches = [harness.patch(GREETING)]
    found = [{"title": f"Bug {n} @octocat", "body": "Closes #1"} for n in range(3)]
    harness.backend.script(Role.WORKER, worker_json(new_issues=found))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    created = harness.writes("create_issue")
    assert len(created) == 2
    for write in created:
        assert "@octocat" not in str(write.args["title"])
        assert str(write.args["body"]).endswith("Found while working on #1.")
        assert "Closes #1" not in str(write.args["body"])
        assert harness.gateway.issues[int(str(write.args["number"]))].labels == {"skeleton HITL"}
    assert_no_forbidden_writes(harness)


async def test_long_comment_block_goes_back_to_the_worker_before_any_push(
    harness: Harness,
) -> None:
    harness.patches = [harness.patch(COMMENTED), harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr", "set_review_check"]
    follow_up = harness.backend.prompts(Role.WORKER)[1]
    assert (
        "app.py:1: comment block of 3 lines, limit is 2 (contestable as `comment_block`)"
        in follow_up.prompt
    )
    [push] = harness.writes("push_branch")
    assert git(harness.remote, "show", f"{push.args['sha']}:app.py").decode() == GREETING["app.py"]


async def test_contested_comment_block_is_judged_by_the_reviewer(harness: Harness) -> None:
    harness.patches = [harness.patch(COMMENTED)]
    harness.backend.script(Role.WORKER, worker_json(), worker_json(objections=[LICENSE_OBJECTION]))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    [review] = harness.backend.prompts(Role.REVIEWER)
    assert (
        "[comment_block] app.py:1: comment block of 3 lines, limit is 2; "
        "worker reason: License header" in review.prompt
    )
    assert harness.actions() == ["push_branch", "set_review_check", "open_pr", "set_review_check"]


async def test_comment_block_contested_up_front_costs_no_policy_round(harness: Harness) -> None:
    harness.patches = [harness.patch(COMMENTED)]
    harness.backend.script(Role.WORKER, worker_json(objections=[LICENSE_OBJECTION]))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 0)


async def test_rejected_contest_goes_back_through_review(harness: Harness) -> None:
    harness.patches = [harness.patch(COMMENTED), harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(objections=[LICENSE_OBJECTION]), worker_json())
    unjustified = {"severity": "major", "path": "app.py", "line": 1, "message": "Not needed."}
    harness.backend.script(
        Role.REVIEWER, review_json("changes_requested", [unjustified]), review_json()
    )

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.review_rounds, task.policy_rounds) == (TaskState.DONE, 1, 0)
    assert "Not needed." in harness.backend.prompts(Role.WORKER)[1].prompt


WORKFLOW_PATH = ".github/workflows/ci.yml"


@pytest.mark.parametrize(
    ("files", "objection"),
    [
        pytest.param(COMMENTED, {**LICENSE_OBJECTION, "line": 2}, id="another-line"),
        pytest.param(
            {WORKFLOW_PATH: "on: push\n"},
            {**LICENSE_OBJECTION, "path": WORKFLOW_PATH},
            id="hard-violation",
        ),
    ],
)
async def test_objection_that_does_not_match_lifts_nothing(
    harness: Harness, files: dict[str, str], objection: dict[str, object]
) -> None:
    harness.set("limits", max_policy_rounds=1)
    harness.patches = [harness.patch(files)]
    harness.backend.script(
        Role.WORKER, worker_json(objections=[objection]), worker_json(objections=[objection])
    )

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "push_branch" not in harness.actions()
    assert harness.backend.prompts(Role.REVIEWER) == []


async def test_objection_for_an_unknown_check_is_invalid_output(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING)]
    unknown = {**LICENSE_OBJECTION, "check": "protected_path"}
    harness.backend.script(Role.WORKER, worker_json(objections=[unknown]))

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "invalid WorkerResult output" in str(task.note)
    assert "push_branch" not in harness.actions()
