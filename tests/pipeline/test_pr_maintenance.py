from datetime import UTC, datetime

import pytest
from factories import seed_run, seed_task
from fake_gateway import FakePull
from git_helpers import commit_files, git, git_text
from pipeline_helpers import (
    BOTH,
    BRANCH,
    HELLO,
    HI,
    PR_NUMBER,
    Harness,
    advance_main,
    assert_no_forbidden_writes,
    is_repo_command,
    only_task,
    open_pr,
    parents_of,
    remote_file,
    review_json,
    worker_json,
    workspace_editor,
)

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.git import GitCommandError
from skeleton_crew.domain.ci_policy import CiState, CiStatus, FailedJob, ReviewCheck
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import worker_key
from skeleton_crew.domain.task_state import TaskKind, TaskState
from skeleton_crew.pipeline.pr_maintenance import MERGEABLE_POLL_SECONDS, MERGEABLE_POLLS
from skeleton_crew.pipeline.texts import RERUN_CI_MESSAGE
from skeleton_crew.store import runs_store

pytestmark = pytest.mark.usefixtures("ready_issue")


async def _skip_summaries(harness: Harness) -> list[str]:
    return [e.summary for e in await harness.audit_events(AuditKind.PR_FIX_SKIPPED)]


async def test_mergeable_pr_gets_no_task_clone_or_quota(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, mergeable=True)
    advance_main(harness, {"README.md": "# Demo v2\n"})

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert await _skip_summaries(harness) == ["PR #10 merges cleanly"]
    assert harness.backend.requests == []
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
    assert git_text(harness.remote, "rev-parse", BRANCH) == head
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
    assert git_text(harness.remote, "rev-parse", BRANCH) == head
    async with harness.sessionmaker() as session:
        since = datetime(2000, 1, 1, tzinfo=UTC)
        assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX) == 0
    assert not (harness.config.tasks_dir / worker_key(task.id) / "controller").exists()


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
    harness.set("repo", check_command="make check")
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
    assert (AuditKind.PR_FIX_SKIPPED, AuditOutcome.INFO) in await harness.audit_kinds()


async def test_pr_only_pushed_in_a_dry_run_counts_as_not_ours(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, recorded_head="b" * 40)
    advance_main(harness, {"app.py": HELLO})
    async with harness.sessionmaker.begin() as session:
        run = await seed_run(session, dry_run=True)
        await seed_task(session, run.id, 1, "Add greeting", state=TaskState.DONE, head_sha=head)

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
    harness.set("limits", conflict_fixes_per_day=0)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.mergeable_reads == []
    assert harness.gateway.writes == []


async def test_fix_quota_counts_only_conflicting_prs(harness: Harness) -> None:
    harness.add_ready_issue(2, "Add farewell")
    await open_pr(harness, {"app.py": HI}, mergeable=True)
    harness.gateway.pulls.append(FakePull(11, "skeleton/2-add-farewell", "c" * 40))
    harness.gateway.mergeable[11] = [False]
    async with harness.sessionmaker.begin() as session:
        run = await seed_run(session)
        await seed_task(session, run.id, 2, "Add farewell", state=TaskState.DONE, head_sha="c" * 40)
    harness.set("limits", conflict_fixes_per_day=1)

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.issue_number, task.kind) == (2, TaskKind.PR_FIX)
    assert harness.gateway.mergeable_reads == [PR_NUMBER, 11]


async def test_fixes_use_their_own_quota(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    harness.set("limits", issues_per_day=0)
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
    harness.set(dry_run=True)

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.writes == []
    assert git_text(harness.remote, "rev-parse", BRANCH) == head
    assert (AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN) in await harness.audit_kinds()


UNFINISHED = [TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED]
FAILED_CI = CiStatus(CiState.FAILURE, [FailedJob("tests", 7, "https://ci/7")])


def tree_of(harness: Harness, sha: str) -> str:
    return git_text(harness.remote, "rev-parse", f"{sha}^{{tree}}")


@pytest.mark.parametrize("state", UNFINISHED)
async def test_unfinished_pr_with_green_ci_is_reviewed(harness: Harness, state: TaskState) -> None:
    head = await open_pr(harness, {"app.py": HI}, state=state, mergeable=True)
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    assert harness.gateway.mergeable_reads == []
    assert git_text(harness.remote, "rev-parse", BRANCH) == head
    assert harness.gateway.review_checks[head][0] is ReviewCheck.PASSED
    assert [r.role for r in harness.backend.requests] == [Role.REVIEWER]
    assert_no_forbidden_writes(harness)


async def test_unfinished_pr_with_red_ci_reruns_it_with_an_empty_commit(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    harness.gateway.ci_default = [FAILED_CI, CiStatus(CiState.SUCCESS, [])]
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.ci_rounds) == (TaskState.DONE, 0)
    [(_, rerun)] = harness.gateway.pushes
    assert parents_of(harness, rerun) == [head]
    assert tree_of(harness, rerun) == tree_of(harness, head)
    message = git_text(harness.remote, "log", "-1", "--format=%s", rerun)
    assert message == RERUN_CI_MESSAGE
    assert harness.gateway.leases == [head]
    assert harness.gateway.review_checks[rerun][0] is ReviewCheck.PASSED
    assert [r.role for r in harness.backend.requests] == [Role.REVIEWER]
    assert_no_forbidden_writes(harness)


async def test_ci_that_fails_again_after_the_rerun_goes_to_the_worker(harness: Harness) -> None:
    await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    harness.gateway.ci_default = [FAILED_CI, FAILED_CI, CiStatus(CiState.SUCCESS, [])]
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
    harness.set("limits", conflict_fixes_per_day=0)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_finishing_a_pr_in_a_dry_run_writes_nothing(harness: Harness) -> None:
    head = await open_pr(harness, {"app.py": HI}, state=TaskState.ESCALATED)
    harness.gateway.ci_default = [FAILED_CI, CiStatus(CiState.SUCCESS, [])]
    harness.backend.script(Role.REVIEWER, review_json())
    harness.set(dry_run=True)

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.writes == []
    assert git_text(harness.remote, "rev-parse", BRANCH) == head
