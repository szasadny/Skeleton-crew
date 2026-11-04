from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_gateway import FakePull
from fake_runtime import ExecCall
from pipeline_helpers import (
    BLOCKER,
    Harness,
    assert_no_forbidden_writes,
    git,
    is_repo_command,
    review_json,
    selection_json,
    worker_json,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.git import GitCommandError
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import RunState, TaskKind, TaskState
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.pipeline.sessions import WORKSPACE
from skeleton_crew.store import runs_store

BRANCH = "skeleton/1-add-greeting"
HI = "def main() -> None:\n    print('hi')\n"
HELLO = "def main() -> None:\n    print('hello')\n"
BOTH = "def main() -> None:\n    print('hi')\n    print('hello')\n"


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    h = Harness(tmp_path, sessionmaker)
    h.add_ready_issue()
    return h


def _commit(repo: Path, files: Mapping[str, str], message: str) -> str:
    for path, content in files.items():
        (repo / path).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode().strip()


async def _open_pr(
    h: Harness,
    files: Mapping[str, str],
    *,
    state: TaskState = TaskState.DONE,
    recorded_head: str | None = None,
) -> str:
    work = h.tmp_path / "branch-work"
    git(h.tmp_path, "clone", "-q", str(h.source), str(work))
    git(work, "checkout", "-q", "-b", BRANCH)
    head = _commit(work, files, "Add greeting")
    git(work, "push", "-q", str(h.remote), f"HEAD:refs/heads/{BRANCH}")
    h.gateway.pulls.append(FakePull(10, BRANCH, head))
    h.gateway.behind[head] = 1
    async with h.sessionmaker() as session:
        run = await runs_store.create_run(session, "manual", False)
        task = await runs_store.add_worker_task(session, run.id, 1, "Add greeting")
        await runs_store.update_worker_task(
            session, task.id, state=state, head_sha=recorded_head or head, branch=BRANCH
        )
        await runs_store.finish_run(session, run.id, RunState.COMPLETED)
        await session.commit()
    return head


def _advance_main(h: Harness, files: Mapping[str, str]) -> str:
    return _commit(h.source, files, "Change main")


def _parents(h: Harness, sha: str) -> list[str]:
    return git(h.remote, "rev-list", "--parents", "-n", "1", sha).decode().split()[1:]


def _remote_file(h: Harness, sha: str, path: str) -> str:
    return git(h.remote, "show", f"{sha}:{path}").decode()


def _workspace_editor(h: Harness, *rounds: Mapping[str, str]) -> None:
    pending = list(rounds)

    def patch(call: ExecCall) -> bytes:
        spec = next(s for s in h.runtime.started if s.name == call.handle.name)
        workspace = next(m.host_path for m in spec.mounts if m.container_path == WORKSPACE)
        files = pending.pop(0) if len(pending) > 1 else pending[0]
        for path, content in files.items():
            (workspace / path).write_text(content)
        git(workspace, "add", "-A")
        return git(workspace, "diff", "--cached", "--binary", call.argv[-1])

    h.patch_from_workspace = patch


async def _only_task(h: Harness, run_id: int) -> WorkerTask:
    [task] = await h.tasks(run_id)
    return task


async def test_clean_update_is_skipped_without_agents_writes_or_quota(harness: Harness) -> None:
    head = await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"README.md": "# Demo v2\n"})

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_UPDATE, TaskState.ABORTED)
    assert task.escalation_reason == "the PR merges cleanly"
    assert harness.backend.requests == []
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() == head
    async with harness.sessionmaker() as session:
        since = datetime(2000, 1, 1, tzinfo=UTC)
        assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX) == 0
    assert not (harness.config.tasks_dir / f"worker-{task.id}" / "controller").exists()


async def test_conflict_is_resolved_by_a_worker_into_a_merge_commit(harness: Harness) -> None:
    head = await _open_pr(harness, {"app.py": HI})
    main = _advance_main(harness, {"app.py": HELLO})
    _workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    [worker] = harness.backend.prompts(Role.WORKER)
    assert "conflict markers" in worker.prompt
    assert "app.py" in worker.prompt
    [(_, merged)] = harness.gateway.pushes
    assert _parents(harness, merged) == [head, main]
    assert _remote_file(harness, merged, "app.py") == BOTH
    assert harness.gateway.leases == [head]
    assert harness.gateway.review_checks[merged][0] is True
    assert [r.role for r in harness.backend.requests] == [Role.WORKER, Role.REVIEWER]
    assert_no_forbidden_writes(harness)


async def test_left_over_conflict_markers_are_sent_back(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    marked = "<<<<<<< ours\n    print('hi')\n=======\n    print('hello')\n>>>>>>> main\n"
    _workspace_editor(harness, {"app.py": "def main() -> None:\n" + marked}, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    _, second = harness.backend.prompts(Role.WORKER)
    assert second.resume_session_id == "worker-session"
    assert "unresolved conflict marker" in second.prompt
    [(_, merged)] = harness.gateway.pushes
    assert _remote_file(harness, merged, "app.py") == BOTH


async def test_resolution_may_not_touch_files_outside_the_conflict(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    _workspace_editor(
        harness,
        {"app.py": BOTH, "README.md": "# Owned\n"},
        {"app.py": BOTH, "README.md": "# Demo\n"},
    )
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.state, task.policy_rounds) == (TaskState.DONE, 1)
    second = harness.backend.prompts(Role.WORKER)[1]
    assert "README.md: changed outside the merge conflict" in second.prompt
    [(_, merged)] = harness.gateway.pushes
    assert _remote_file(harness, merged, "README.md") == "# Demo\n"


async def test_check_command_waits_until_the_conflict_is_resolved(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    harness.repo(check_command="make check")
    _workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    assert not any(is_repo_command(call.argv) for call in harness.runtime.execs)


async def test_unresolvable_conflict_escalates_the_issue(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    _workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json("blocked", note="Both sides conflict."))

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert harness.gateway.pushes == []
    assert harness.gateway.issues[1].labels == {"skeleton HITL"}
    assert_no_forbidden_writes(harness)


@pytest.mark.parametrize("label", ["skeleton HITL", "skeleton refine"])
async def test_pr_is_left_alone_without_the_ready_label(harness: Harness, label: str) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    harness.gateway.issues[1].labels.add(label)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_pr_is_left_alone_once_the_ready_label_is_removed(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    harness.gateway.issues[1].labels.clear()

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_pr_with_commits_from_someone_else_is_skipped(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI}, recorded_head="b" * 40)
    _advance_main(harness, {"app.py": HELLO})

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []
    assert (AuditKind.PR_UPDATE_SKIPPED, AuditOutcome.INFO) in await harness.audit_kinds()


async def test_pr_only_pushed_in_a_dry_run_counts_as_not_ours(harness: Harness) -> None:
    head = await _open_pr(harness, {"app.py": HI}, recorded_head="b" * 40)
    _advance_main(harness, {"app.py": HELLO})
    async with harness.sessionmaker() as session:
        run = await runs_store.create_run(session, "manual", True)
        task = await runs_store.add_worker_task(session, run.id, 1, "Add greeting")
        await runs_store.update_worker_task(session, task.id, state=TaskState.DONE, head_sha=head)
        await session.commit()

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []


async def test_branch_moved_after_listing_aborts_without_writes(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"README.md": "# Demo v2\n"})
    work = harness.tmp_path / "branch-work"
    _commit(work, {"app.py": BOTH}, "Human fix")
    git(work, "push", "-q", str(harness.remote), f"HEAD:refs/heads/{BRANCH}")

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert task.state is TaskState.ABORTED
    assert harness.gateway.writes == []


async def test_failed_push_keeps_the_old_head_as_ours(harness: Harness) -> None:
    head = await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    _workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.gateway.failures["push_branch"] = GitCommandError("stale info")

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.state, task.head_sha) == (TaskState.FAILED, None)
    async with harness.sessionmaker() as session:
        last = await runs_store.last_pushed_task(session, 1)
    assert last is not None
    assert last.head_sha == head


async def test_up_to_date_pr_is_not_touched(harness: Harness) -> None:
    head = await _open_pr(harness, {"app.py": HI})
    harness.gateway.behind[head] = 0

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []


async def test_no_fix_budget_means_no_update(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    harness.limits(conflict_fixes_per_day=0)

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []


async def test_fixes_use_their_own_quota(harness: Harness) -> None:
    await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    harness.limits(issues_per_day=0)
    _workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    async with harness.sessionmaker() as session:
        since = datetime(2000, 1, 1, tzinfo=UTC)
        assert await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX) == 1


async def test_dry_run_update_writes_nothing(harness: Harness) -> None:
    head = await _open_pr(harness, {"app.py": HI})
    _advance_main(harness, {"app.py": HELLO})
    _workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())
    harness.settings = harness.settings.model_copy(update={"dry_run": True})

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.writes == []
    assert git(harness.remote, "rev-parse", BRANCH).decode().strip() == head
    assert (AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN) in await harness.audit_kinds()


def _push_branch_without_pr(h: Harness, files: Mapping[str, str]) -> str:
    work = h.tmp_path / "left-over"
    git(h.tmp_path, "clone", "-q", str(h.source), str(work))
    if files:
        _commit(work, files, "Earlier work")
    head = git(work, "rev-parse", "HEAD").decode().strip()
    git(work, "push", "-q", str(h.remote), f"{head}:refs/heads/{BRANCH}")
    h.backend.script(Role.SELECTOR, selection_json([1]))
    return head


async def test_existing_branch_is_continued_with_a_comment_and_a_new_pr(harness: Harness) -> None:
    head = _push_branch_without_pr(harness, {"app.py": HI})
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
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
    head = _push_branch_without_pr(harness, {"app.py": HI})
    main = _advance_main(harness, {"README.md": "# Demo v2\n"})
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert task.state is TaskState.DONE
    merged = str(task.head_sha)
    assert _parents(harness, merged) == [head, main]
    assert _remote_file(harness, merged, "app.py") == HI
    assert harness.gateway.leases == [head]


async def test_reviewer_findings_on_a_continued_branch_go_to_a_worker(harness: Harness) -> None:
    head = _push_branch_without_pr(harness, {"app.py": HI})
    _workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(
        Role.REVIEWER,
        review_json("changes_requested", [BLOCKER]),
        review_json(),
    )
    harness.backend.script(Role.WORKER, worker_json(pr_title="Unused title"))

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.ISSUE, TaskState.DONE)
    [worker] = harness.backend.prompts(Role.WORKER)
    assert "the reviewer requested changes" in worker.prompt
    [pr] = harness.writes("open_pr")
    assert pr.args["title"] == "[skeleton] Add greeting"
    first, fixed = (sha for _, sha in harness.gateway.pushes)
    assert first == head
    assert harness.gateway.leases == [head, head]
    assert _parents(harness, fixed) == [head]
    assert _remote_file(harness, fixed, "app.py") == BOTH


async def test_existing_branch_without_own_changes_gets_a_fresh_worker(harness: Harness) -> None:
    old = _push_branch_without_pr(harness, {})
    main = _advance_main(harness, {"README.md": "# Demo v2\n"})
    _workspace_editor(harness, {"app.py": HI})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert (task.kind, task.state) == (TaskKind.ISSUE, TaskState.DONE)
    [worker] = harness.backend.prompts(Role.WORKER)
    assert "update a pull request" not in worker.prompt
    assert harness.gateway.leases == [old]
    assert _parents(harness, str(task.head_sha)) == [main]
    assert "comment" not in harness.actions()


async def test_new_branch_is_pushed_only_if_it_still_does_not_exist(harness: Harness) -> None:
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    _workspace_editor(harness, {"app.py": HI})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await _only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.leases == [None]
    assert "comment" not in harness.actions()
