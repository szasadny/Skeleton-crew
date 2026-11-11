from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_gateway import FakePull
from pipeline_helpers import (
    BOTH,
    BRANCH,
    HELLO,
    HI,
    PR_NUMBER,
    Harness,
    advance_main,
    assert_no_forbidden_writes,
    commit_files,
    git,
    is_repo_command,
    only_task,
    open_pr,
    parents_of,
    remote_file,
    review_json,
    worker_json,
    workspace_editor,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.git import GitCommandError
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskKind, TaskState
from skeleton_crew.github.gateway import CiState, CiStatus, FailedJob, ReviewCheck
from skeleton_crew.pipeline.pr_maintenance import MERGEABLE_POLL_SECONDS, MERGEABLE_POLLS
from skeleton_crew.pipeline.texts import RERUN_CI_MESSAGE
from skeleton_crew.store import audit_store, runs_store


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    h = Harness(tmp_path, sessionmaker)
    h.add_ready_issue()
    return h


async def _skip_summaries(harness: Harness) -> list[str]:
    async with harness.sessionmaker() as session:
        events = await audit_store.list_events(session, 500, 0)
    return [e.summary for e in events if e.kind is AuditKind.PR_UPDATE_SKIPPED]


async def test_mergeable_pr_gets_no_task_clone_or_quota(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, mergeable=True)
    advance_main(harness, {"README.md": "# Demo v2\n"})

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert await _skip_summaries(harness) == ["PR #10 merges cleanly"]
    assert harness.backend.requests == []
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() == head
    assert not list(harness.config.tasks_dir.glob("worker-*"))


async def test_unknown_mergeability_is_polled_then_skipped(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI}, mergeable=None)
    advance_main(harness, {"app.py": HELLO})

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.mergeable_reads == [PR_NUMBER] * MERGEABLE_POLLS
    assert harness.sleeps == [MERGEABLE_POLL_SECONDS] * (MERGEABLE_POLLS - 1)
    assert await _skip_summaries(harness) == ["PR #10: mergeability unknown"]
    assert harness.gateway.writes == []
    assert harness.backend.requests == []


async def test_mergeability_computed_on_a_later_poll_starts_a_fix(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    harness.gateway.mergeable[PR_NUMBER] = [None, False]
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    assert harness.gateway.mergeable_reads == [PR_NUMBER, PR_NUMBER]


async def test_failed_mergeability_read_skips_the_pr(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    harness.gateway.failures["pr_mergeable"] = RuntimeError("boom")

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_pr_that_merges_cleanly_after_all_ends_without_writes(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, mergeable=False)
    advance_main(harness, {"README.md": "# Demo v2\n"})

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.ABORTED)
    assert task.note == "the PR merges cleanly after all"
    assert harness.backend.requests == []
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() == head
    async with harness.sessionmaker() as session:
        since = datetime(2000, 1, 1, tzinfo=UTC)
        assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX) == 0
    assert not (harness.config.tasks_dir / f"worker-{task.id}" / "controller").exists()


async def test_conflict_is_resolved_by_a_worker_into_a_merge_commit(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI})
    main = advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    [worker] = harness.backend.prompts(Role.WORKER)
    assert "conflict markers" in worker.prompt
    assert "app.py" in worker.prompt
    [(_, merged)] = harness.gateway.pushes
    assert parents_of(harness, merged) == [head, main]
    assert remote_file(harness, merged, "app.py") == BOTH
    assert harness.gateway.leases == [head]
    assert harness.gateway.review_checks[merged][0] is ReviewCheck.PASSED
    assert [r.role for r in harness.backend.requests] == [Role.WORKER, Role.REVIEWER]
    assert_no_forbidden_writes(harness)


async def test_left_over_conflict_markers_are_sent_back(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    marked = "<<<<<<< ours\n    print('hi')\n=======\n    print('hello')\n>>>>>>> main\n"
    workspace_editor(harness, {"app.py": "def main() -> None:\n" + marked}, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    _, second = harness.backend.prompts(Role.WORKER)
    assert second.resume_session_id == "worker-session"
    assert "unresolved conflict marker" in second.prompt
    [(_, merged)] = harness.gateway.pushes
    assert remote_file(harness, merged, "app.py") == BOTH


async def test_resolution_may_not_touch_files_outside_the_conflict(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(
        harness,
        {"app.py": BOTH, "README.md": "# Owned\n"},
        {"app.py": BOTH, "README.md": "# Demo\n"},
    )
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    second = harness.backend.prompts(Role.WORKER)[1]
    assert "README.md: changed outside the merge conflict" in second.prompt
    [(_, merged)] = harness.gateway.pushes
    assert remote_file(harness, merged, "README.md") == "# Demo\n"


async def test_check_command_waits_until_the_conflict_is_resolved(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    harness.repo(check_command="make check")
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    assert not any(is_repo_command(call.argv) for call in harness.runtime.execs)


async def test_unresolvable_conflict_escalates_the_issue(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json("blocked", note="Both sides conflict."))

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert harness.gateway.pushes == []
    assert harness.gateway.issues[1].labels == {"skeleton HITL"}
    assert_no_forbidden_writes(harness)


@pytest.mark.parametrize("label", ["skeleton HITL", "skeleton refine"])
async def test_pr_is_left_alone_without_the_ready_label(harness: Harness, label: str) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    harness.gateway.issues[1].labels.add(label)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_pr_is_left_alone_once_the_ready_label_is_removed(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    harness.gateway.issues[1].labels.clear()

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_pr_with_commits_from_someone_else_is_skipped(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI}, recorded_head="b" * 40)
    advance_main(harness, {"app.py": HELLO})

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []
    assert (AuditKind.PR_UPDATE_SKIPPED, AuditOutcome.INFO) in await harness.audit_kinds()


async def test_pr_only_pushed_in_a_dry_run_counts_as_not_ours(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, recorded_head="b" * 40)
    advance_main(harness, {"app.py": HELLO})
    async with harness.sessionmaker() as session:
        run = await runs_store.create_run(session, "manual", True)
        task = await runs_store.add_worker_task(session, run.id, 1, "Add greeting")
        await runs_store.update_worker_task(session, task.id, state=TaskState.DONE, head_sha=head)
        await session.commit()

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []


async def test_branch_moved_after_listing_aborts_without_writes(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"README.md": "# Demo v2\n"})
    work = harness.tmp_path / "branch-work"
    commit_files(work, {"app.py": BOTH}, "Human fix")
    git(work, "push", "-q", str(harness.remote), f"HEAD:refs/heads/{BRANCH}")

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ABORTED
    assert harness.gateway.writes == []


async def test_failed_push_keeps_the_old_head_as_ours(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.gateway.failures["push_branch"] = GitCommandError("stale info")

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.head_sha) == (TaskState.FAILED, None)
    async with harness.sessionmaker() as session:
        last = await runs_store.last_pushed_task(session, 1)
    assert last is not None
    assert last.head_sha == head


async def test_no_fix_budget_means_no_update(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    harness.limits(conflict_fixes_per_day=0)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.mergeable_reads == []
    assert harness.gateway.writes == []


async def test_fix_quota_counts_only_conflicting_prs(harness: Harness) -> None:
    harness.add_ready_issue(2, "Add farewell")
    await open_pr(harness, {"app.py": HI}, mergeable=True)
    harness.gateway.pulls.append(FakePull(11, "skeleton/2-add-farewell", "c" * 40))
    harness.gateway.mergeable[11] = [False]
    async with harness.sessionmaker() as session:
        run = await runs_store.create_run(session, "manual", False)
        task = await runs_store.add_worker_task(session, run.id, 2, "Add farewell")
        await runs_store.update_worker_task(
            session, task.id, state=TaskState.DONE, head_sha="c" * 40
        )
        await session.commit()
    harness.limits(conflict_fixes_per_day=1)

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.issue_number, task.kind) == (2, TaskKind.PR_FIX)
    assert harness.gateway.mergeable_reads == [PR_NUMBER, 11]


async def test_fixes_use_their_own_quota(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    harness.limits(issues_per_day=0)
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    async with harness.sessionmaker() as session:
        since = datetime(2000, 1, 1, tzinfo=UTC)
        assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX) == 1


async def test_dry_run_update_writes_nothing(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())
    harness.settings = harness.settings.model_copy(update={"dry_run": True})

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.writes == []
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() == head
    assert (AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN) in await harness.audit_kinds()


UNFINISHED = [TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED]
FAILED_CI = CiStatus(CiState.FAILURE, [FailedJob("tests", 7, "https://ci/7")])


def tree_of(harness: Harness, sha: str) -> str:
    return git(harness.remote, "rev-parse", f"{sha}^{{tree}}").decode().strip()


@pytest.mark.parametrize("state", UNFINISHED)
async def test_unfinished_pr_with_green_ci_is_reviewed(harness: Harness, state: TaskState) -> None:
    head = await open_pr(harness, {"app.py": HI}, state=state, mergeable=True)
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    assert harness.gateway.mergeable_reads == []
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() == head
    assert harness.gateway.review_checks[head][0] is ReviewCheck.PASSED
    assert [r.role for r in harness.backend.requests] == [Role.REVIEWER]
    assert_no_forbidden_writes(harness)


async def test_unfinished_pr_with_red_ci_reruns_it_with_an_empty_commit(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    harness.gateway.ci_queue = [FAILED_CI, CiStatus(CiState.SUCCESS, [])]
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.ci_rounds) == (TaskState.DONE, 0)
    [(_, rerun)] = harness.gateway.pushes
    assert parents_of(harness, rerun) == [head]
    assert tree_of(harness, rerun) == tree_of(harness, head)
    message = git(harness.remote, "log", "-1", "--format=%s", rerun).decode().strip()
    assert message == RERUN_CI_MESSAGE
    assert harness.gateway.leases == [head]
    assert harness.gateway.review_checks[rerun][0] is ReviewCheck.PASSED
    assert [r.role for r in harness.backend.requests] == [Role.REVIEWER]
    assert_no_forbidden_writes(harness)


async def test_ci_that_fails_again_after_the_rerun_goes_to_the_worker(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    harness.gateway.ci_queue = [FAILED_CI, FAILED_CI, CiStatus(CiState.SUCCESS, [])]
    harness.gateway.job_logs[7] = "AssertionError: boom"
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.ci_rounds) == (TaskState.DONE, 1)
    [worker] = harness.backend.prompts(Role.WORKER)
    assert worker.resume_session_id is None
    assert "AssertionError: boom" in worker.prompt
    rerun, fixed = (sha for _, sha in harness.gateway.pushes)
    assert parents_of(harness, fixed) == [rerun]
    assert remote_file(harness, fixed, "app.py") == BOTH
    assert_no_forbidden_writes(harness)


async def test_unfinished_pr_behind_the_base_gets_a_merge_commit(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    main = advance_main(harness, {"README.md": "# Demo v2\n"})
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    [(_, merged)] = harness.gateway.pushes
    assert parents_of(harness, merged) == [head, main]
    assert harness.gateway.review_checks[merged][0] is ReviewCheck.PASSED
    assert [r.role for r in harness.backend.requests] == [Role.REVIEWER]


async def test_unfinished_pr_with_commits_from_someone_else_is_not_finished(
    harness: Harness,
) -> None:
    await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED, recorded_head="b" * 40)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []
    assert harness.backend.requests == []
    assert await _skip_summaries(harness) == ["PR #10 has commits Skeleton Crew did not push"]


async def test_unfinished_pr_needs_fix_quota(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    harness.limits(conflict_fixes_per_day=0)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_finishing_a_pr_in_a_dry_run_writes_nothing(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    harness.gateway.ci_queue = [FAILED_CI, CiStatus(CiState.SUCCESS, [])]
    harness.backend.script(Role.REVIEWER, review_json())
    harness.settings = harness.settings.model_copy(update={"dry_run": True})

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.writes == []
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() == head
