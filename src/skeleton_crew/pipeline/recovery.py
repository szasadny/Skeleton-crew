import logging
from collections.abc import Sequence

from skeleton_crew.domain.task_state import RefineState, RunState, TaskEvent, transition
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.pipeline.texts import INTERRUPTED_COMMENT
from skeleton_crew.runtime.containers import MANAGED_LABEL, MANAGED_VALUE
from skeleton_crew.store import runs_store

KILLED_NOTE = "killed"
RESTART_NOTE = "interrupted by a controller restart"

log = logging.getLogger(__name__)


async def remove_managed_containers(deps: PipelineDeps) -> None:
    try:
        await deps.runtime.remove_labeled(MANAGED_LABEL, MANAGED_VALUE)
    except Exception:
        log.exception("could not remove managed containers")


async def abort_leftovers(deps: PipelineDeps, note: str) -> list[int]:
    live_issues: list[int] = []
    async with deps.sessionmaker() as session:
        for task in await runs_store.non_terminal_worker_tasks(session):
            state = transition(task.state, TaskEvent.ABORT)
            await runs_store.update_worker_task(session, task.id, state=state, note=note)
            run = await runs_store.get_run(session, task.run_id)
            if run is not None and not run.dry_run:
                live_issues.append(task.issue_number)
        for refine in await runs_store.non_terminal_refine_tasks(session):
            await runs_store.update_refine_task(
                session, refine.id, state=RefineState.ABORTED, error=note
            )
        for run in await runs_store.running_runs(session):
            await runs_store.finish_run(session, run.id, RunState.ABORTED, note)
        await session.commit()
    return sorted(set(live_issues))


async def comment_interrupted(deps: PipelineDeps, issues: Sequence[int]) -> None:
    try:
        gw = deps.gateway(await deps.load_settings())
        for number in issues:
            await gw.comment(number, INTERRUPTED_COMMENT)
    except Exception:
        log.exception("could not comment on interrupted issues")
