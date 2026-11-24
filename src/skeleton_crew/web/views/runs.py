from dataclasses import dataclass
from typing import Self

from skeleton_crew.domain.task_keys import refine_key, selector_key, worker_key
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.services.errors import NotFound
from skeleton_crew.store import runs_store
from skeleton_crew.store.runs_store import RunWithTasks
from skeleton_crew.web.errors import found
from skeleton_crew.web.state import SessionDep


@dataclass(frozen=True)
class RunView:
    run: Run
    tasks: list[WorkerTask]
    refines: list[RefineTask]
    selector_key: str
    worker_keys: dict[int, str]
    refine_keys: dict[int, str]

    @classmethod
    def of(cls, loaded: RunWithTasks) -> Self:
        return cls(
            run=loaded.run,
            tasks=loaded.tasks,
            refines=loaded.refines,
            selector_key=selector_key(loaded.run.id),
            worker_keys={task.id: worker_key(task.id) for task in loaded.tasks},
            refine_keys={task.id: refine_key(task.id) for task in loaded.refines},
        )

    def has_key(self, key: str) -> bool:
        return (
            key == self.selector_key
            or key in self.worker_keys.values()
            or key in self.refine_keys.values()
        )

    def context(self) -> dict[str, object]:
        return dict(vars(self))


async def load_run(session: SessionDep, run_id: int, key: str | None = None) -> RunView:
    view = RunView.of(found(await runs_store.run_with_tasks(session, run_id)))
    if key is not None and not view.has_key(key):
        raise NotFound(key)
    return view
