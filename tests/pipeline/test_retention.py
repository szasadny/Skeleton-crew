from datetime import datetime, timedelta
from pathlib import Path

import pytest
from clocks import START
from factories import seed_refine, seed_run, seed_task
from pipeline_helpers import Harness

from skeleton_crew.domain.task_keys import refine_key, selector_key, worker_key
from skeleton_crew.domain.task_state import RefineState, RunState, TaskState
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.store import runs_store

OLD = START - timedelta(days=31)
RECENT = START - timedelta(days=29)


async def _past_run(h: Harness, at: datetime, state: RunState = RunState.COMPLETED) -> int:
    async with h.sessionmaker.begin() as session:
        run = await seed_run(session)
        if state is not RunState.RUNNING:
            finished = await runs_store.finish_run(session, run.id, state)
            finished.finished_at = at
        return run.id


async def _worker_task(h: Harness, run_id: int, state: TaskState, at: datetime) -> int:
    async with h.sessionmaker.begin() as session:
        task = await seed_task(session, run_id, 1, "Old work", state=state)
        task.updated_at = at
        return task.id


async def _refine_task(h: Harness, run_id: int, state: RefineState, at: datetime) -> int:
    async with h.sessionmaker.begin() as session:
        task = await seed_refine(session, run_id, 2, state=state)
        task.updated_at = at
        return task.id


def _task_dir(h: Harness, key: str) -> Path:
    transcripts = h.config.tasks_dir / key / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "worker-1.jsonl").write_text("{}\n")
    return transcripts.parent


async def test_expired_terminal_task_dirs_are_removed(harness: Harness) -> None:
    old_run = await _past_run(harness, OLD)
    old_worker = await _worker_task(harness, old_run, TaskState.DONE, OLD)
    old_refine = await _refine_task(harness, old_run, RefineState.FAILED, OLD)
    removed = [
        _task_dir(harness, worker_key(old_worker)),
        _task_dir(harness, refine_key(old_refine)),
        _task_dir(harness, selector_key(old_run)),
    ]

    run_id = await harness.run()

    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    assert not any(path.exists() for path in removed)
    async with harness.sessionmaker() as session:
        assert await session.get(WorkerTask, old_worker) is not None
        assert await session.get(RefineTask, old_refine) is not None


async def test_recent_and_non_terminal_task_dirs_are_kept(harness: Harness) -> None:
    recent_run = await _past_run(harness, RECENT)
    live_run = await _past_run(harness, OLD, RunState.RUNNING)
    recent = await _worker_task(harness, recent_run, TaskState.DONE, RECENT)
    coding = await _worker_task(harness, live_run, TaskState.CODING, OLD)
    refining = await _refine_task(harness, live_run, RefineState.REFINING, OLD)
    kept = [
        _task_dir(harness, key)
        for key in (
            worker_key(recent),
            worker_key(coding),
            refine_key(refining),
            selector_key(recent_run),
            selector_key(live_run),
        )
    ]

    await harness.run()

    assert all(path.exists() for path in kept)


async def test_dir_of_a_row_that_is_not_terminal_is_kept(harness: Harness) -> None:
    run = await _past_run(harness, OLD)
    awaiting = await _worker_task(harness, run, TaskState.AWAITING_CI, OLD)
    refining = await _refine_task(harness, run, RefineState.REFINING, OLD)
    kept = [_task_dir(harness, worker_key(awaiting)), _task_dir(harness, refine_key(refining))]
    expired = _task_dir(harness, selector_key(run))

    await harness.run()

    assert all(path.exists() for path in kept)
    assert not expired.exists()


async def test_retention_setting_moves_the_cutoff(harness: Harness) -> None:
    run = await _past_run(harness, RECENT)
    task_dir = _task_dir(
        harness, worker_key(await _worker_task(harness, run, TaskState.DONE, RECENT))
    )
    harness.set("limits", task_retention_days=7)

    await harness.run()

    assert not task_dir.exists()


async def test_symlink_named_like_an_expired_task_is_not_followed(
    harness: Harness, tmp_path: Path
) -> None:
    run = await _past_run(harness, OLD)
    task = await _worker_task(harness, run, TaskState.DONE, OLD)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    harness.config.tasks_dir.mkdir(parents=True)
    link = harness.config.tasks_dir / worker_key(task)
    link.symlink_to(outside, target_is_directory=True)

    await harness.run()

    assert link.is_symlink()
    assert (outside / "keep.txt").read_text() == "keep"


async def test_directories_without_an_expired_row_are_untouched(harness: Harness) -> None:
    run = await _past_run(harness, OLD)
    unknown = [_task_dir(harness, worker_key(999)), _task_dir(harness, f"run-{run}-other")]

    await harness.run()

    assert all(path.exists() for path in unknown)


async def test_cleanup_error_does_not_fail_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(tasks_dir: Path, keys: object) -> list[str]:
        raise OSError("disk gone")

    monkeypatch.setattr("skeleton_crew.pipeline.retention.remove_task_dirs", broken)
    run = await _past_run(harness, OLD)
    task_dir = _task_dir(harness, worker_key(await _worker_task(harness, run, TaskState.DONE, OLD)))

    run_id = await harness.run()

    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    assert task_dir.exists()


async def test_failed_preflight_prunes_nothing(harness: Harness) -> None:
    run = await _past_run(harness, OLD)
    task_dir = _task_dir(harness, worker_key(await _worker_task(harness, run, TaskState.DONE, OLD)))
    harness.backend.problems = ["no credential"]

    run_id = await harness.run()

    assert (await harness.run_row(run_id)).state is RunState.PREFLIGHT_FAILED
    assert task_dir.exists()
