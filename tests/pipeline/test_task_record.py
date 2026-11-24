from factories import seed_run, seed_task
from pipeline_helpers import Harness, only_task

from skeleton_crew.pipeline.task_record import MAX_STORED_BLOCKERS, WorkerTaskRecord


async def test_task_record_dedupes_and_caps_blockers(harness: Harness) -> None:
    async with harness.sessionmaker.begin() as session:
        run = await seed_run(session, dry_run=True)
        task = await seed_task(session, run.id, 1, "Add greeting")
    record = WorkerTaskRecord(harness.sessionmaker, task.id)
    await record.add_blockers(["worker: a", "worker: a", "reviewer: a"])
    await record.add_blockers(["worker: a"])
    await record.add_blockers([f"worker: {n}" for n in range(50)])

    stored = await only_task(harness, run.id)
    assert stored.blockers is not None
    assert stored.blockers[:3] == ["worker: a", "reviewer: a", "worker: 0"]
    assert len(stored.blockers) == MAX_STORED_BLOCKERS
    assert stored.blockers == record.blockers
