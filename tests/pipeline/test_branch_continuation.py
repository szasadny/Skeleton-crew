from pathlib import Path

import pytest
from fake_gateway import FakePull
from pipeline_helpers import (
    BLOCKER,
    BOTH,
    BRANCH,
    HI,
    Harness,
    advance_main,
    assert_no_forbidden_writes,
    commit_files,
    git,
    only_task,
    parents_of,
    push_branch_without_pr,
    record_push,
    remote_file,
    review_json,
    selection_json,
    worker_json,
    workspace_editor,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskKind, TaskState
from skeleton_crew.store import audit_store

FOREIGN = "the branch has commits Skeleton Crew did not push"


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    h = Harness(tmp_path, sessionmaker)
    h.add_ready_issue()
    return h


async def test_existing_branch_is_continued_with_a_comment_and_a_new_pr(harness: Harness) -> None:
    head = await push_branch_without_pr(harness, {"app.py": HI})
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
    head = await push_branch_without_pr(harness, {"app.py": HI})
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
    head = await push_branch_without_pr(harness, {"app.py": HI})
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
    old = await push_branch_without_pr(harness, {})
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


async def _assert_escalated_untouched(harness: Harness, run_id: int, reason: str) -> None:
    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert task.note == f"`{BRANCH}`: {reason}"
    assert harness.actions() == ["comment", "change_labels"]
    [comment] = harness.writes("comment")
    assert reason in str(comment.args["body"])
    assert harness.gateway.pushes == []
    assert harness.gateway.review_checks == {}
    assert harness.writes("open_pr") == []
    assert [spec.labels["skeleton-crew.role"] for spec in harness.runtime.started] == ["selector"]
    assert [r.role for r in harness.backend.requests] == [Role.SELECTOR]
    assert harness.gateway.issues[1].labels == {"skeleton HITL"}
    assert_no_forbidden_writes(harness)


async def test_branch_someone_else_pushed_is_escalated(harness: Harness) -> None:
    await push_branch_without_pr(harness, {"app.py": HI}, recorded=False)

    run_id = await harness.run()

    await _assert_escalated_untouched(harness, run_id, FOREIGN)


async def test_foreign_commit_on_top_of_our_push_is_escalated(harness: Harness) -> None:
    ours = await push_branch_without_pr(harness, {"app.py": HI})
    work = harness.tmp_path / "left-over"
    commit_files(work, {"app.py": BOTH}, "Someone else")
    git(work, "push", "-q", str(harness.remote), f"HEAD:refs/heads/{BRANCH}")

    run_id = await harness.run()

    await _assert_escalated_untouched(harness, run_id, FOREIGN)
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() != ours


async def test_foreign_branch_with_a_secret_never_becomes_a_pr(harness: Harness) -> None:
    await push_branch_without_pr(
        harness, {"config.py": "AWS_SECRET_ACCESS_KEY = 'x'\n"}, recorded=False
    )
    harness.secret_findings = ["aws-secret"]

    run_id = await harness.run()

    await _assert_escalated_untouched(harness, run_id, FOREIGN)


async def test_branch_only_recorded_in_a_dry_run_is_escalated(harness: Harness) -> None:
    head = await push_branch_without_pr(harness, {"app.py": HI}, recorded=False)
    await record_push(harness, head, dry_run=True)

    run_id = await harness.run()

    await _assert_escalated_untouched(harness, run_id, FOREIGN)


async def test_our_branch_with_a_closed_unmerged_pr_is_escalated(harness: Harness) -> None:
    head = await push_branch_without_pr(harness, {"app.py": HI})
    harness.gateway.pulls.append(FakePull(10, BRANCH, head, state="closed"))

    run_id = await harness.run()

    await _assert_escalated_untouched(harness, run_id, "PR #10 was closed without merging")


async def test_our_branch_after_a_merged_pr_gets_a_fresh_worker(harness: Harness) -> None:
    old = await push_branch_without_pr(harness, {"app.py": HI})
    harness.gateway.pulls.append(FakePull(10, BRANCH, old, state="closed", merged=True))
    main = advance_main(harness, {"app.py": HI})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.ISSUE, TaskState.DONE)
    assert harness.gateway.leases == [old]
    assert parents_of(harness, str(task.head_sha)) == [main]
    assert "comment" not in harness.actions()


async def test_two_open_skeleton_prs_exclude_the_issue(harness: Harness) -> None:
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.gateway.pulls.append(FakePull(10, BRANCH, "a" * 40))
    harness.gateway.pulls.append(FakePull(11, "skeleton/1-renamed", "b" * 40))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.backend.requests == []
    assert harness.gateway.writes == []
    assert harness.gateway.mergeable_reads == []
    async with harness.sessionmaker() as session:
        events = await audit_store.list_events(session, 500, 0)
    [skipped] = [e for e in events if e.kind is AuditKind.ISSUE_SKIPPED]
    assert skipped.summary == "#1 has 2 open skeleton PRs"
    assert skipped.payload == {"issue": 1, "prs": [10, 11]}
