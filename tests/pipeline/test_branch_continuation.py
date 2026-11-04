from pathlib import Path

import pytest
from pipeline_helpers import (
    BLOCKER,
    BOTH,
    BRANCH,
    HI,
    Harness,
    advance_main,
    assert_no_forbidden_writes,
    only_task,
    parents_of,
    push_branch_without_pr,
    remote_file,
    review_json,
    selection_json,
    worker_json,
    workspace_editor,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskKind, TaskState


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    h = Harness(tmp_path, sessionmaker)
    h.add_ready_issue()
    return h


async def test_existing_branch_is_continued_with_a_comment_and_a_new_pr(harness: Harness) -> None:
    head = push_branch_without_pr(harness, {"app.py": HI})
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state, task.head_sha) == (TaskKind.ISSUE, TaskState.DONE, head)
    assert [r.role for r in harness.backend.requests] == [Role.SELECTOR, Role.REVIEWER]
    assert harness.actions() == ["comment", "push_branch", "open_pr", "set_review_check"]
    [comment] = harness.writes("comment")
    assert f"existing branch `{BRANCH}`" in str(comment.args["body"])
    [pr] = harness.writes("open_pr")
    assert pr.args["title"] == "[skeleton] Add greeting"
    assert harness.gateway.leases == [head]
    assert harness.gateway.review_checks[head][0] is True
    assert_no_forbidden_writes(harness)


async def test_continued_branch_behind_main_gets_main_merged_in(harness: Harness) -> None:
    head = push_branch_without_pr(harness, {"app.py": HI})
    main = advance_main(harness, {"README.md": "# Demo v2\n"})
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    merged = str(task.head_sha)
    assert parents_of(harness, merged) == [head, main]
    assert remote_file(harness, merged, "app.py") == HI
    assert harness.gateway.leases == [head]


async def test_reviewer_findings_on_a_continued_branch_go_to_a_worker(harness: Harness) -> None:
    head = push_branch_without_pr(harness, {"app.py": HI})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(
        Role.REVIEWER,
        review_json("changes_requested", [BLOCKER]),
        review_json(),
    )
    harness.backend.script(Role.WORKER, worker_json(pr_title="Unused title"))

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.ISSUE, TaskState.DONE)
    [worker] = harness.backend.prompts(Role.WORKER)
    assert "the reviewer requested changes" in worker.prompt
    [pr] = harness.writes("open_pr")
    assert pr.args["title"] == "[skeleton] Add greeting"
    first, fixed = (sha for _, sha in harness.gateway.pushes)
    assert first == head
    assert harness.gateway.leases == [head, head]
    assert parents_of(harness, fixed) == [head]
    assert remote_file(harness, fixed, "app.py") == BOTH


async def test_existing_branch_without_own_changes_gets_a_fresh_worker(harness: Harness) -> None:
    old = push_branch_without_pr(harness, {})
    main = advance_main(harness, {"README.md": "# Demo v2\n"})
    workspace_editor(harness, {"app.py": HI})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.ISSUE, TaskState.DONE)
    [worker] = harness.backend.prompts(Role.WORKER)
    assert "update a pull request" not in worker.prompt
    assert harness.gateway.leases == [old]
    assert parents_of(harness, str(task.head_sha)) == [main]
    assert "comment" not in harness.actions()


async def test_new_branch_is_pushed_only_if_it_still_does_not_exist(harness: Harness) -> None:
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    workspace_editor(harness, {"app.py": HI})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.leases == [None]
    assert "comment" not in harness.actions()
