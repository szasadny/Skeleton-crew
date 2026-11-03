import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.store import runs_store
from skeleton_crew.web.rendering import render
from skeleton_crew.web.state import get_state
from skeleton_crew.web.transcripts import list_transcripts, read_transcript

router = APIRouter()

PAGE_SIZE = 25


@dataclass(frozen=True)
class _RunView:
    run: Run
    tasks: list[WorkerTask]
    refines: list[RefineTask]

    @property
    def transcript_keys(self) -> set[str]:
        return {
            f"run-{self.run.id}-selector",
            *(f"worker-{task.id}" for task in self.tasks),
            *(f"refine-{task.id}" for task in self.refines),
        }

    def context(self, request: Request) -> dict[str, object]:
        repo = get_state(request).config.repo
        return {"run": self.run, "tasks": self.tasks, "refines": self.refines, "repo": repo}


async def _load_run(request: Request, run_id: int, key: str | None = None) -> _RunView:
    async with get_state(request).sessionmaker() as session:
        run = await runs_store.get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=404)
        view = _RunView(
            run,
            await runs_store.list_tasks(session, run_id),
            await runs_store.list_refine_tasks(session, run_id),
        )
    if key is not None and key not in view.transcript_keys:
        raise HTTPException(status_code=404)
    return view


@router.get("/runs")
async def runs(request: Request, page: int = Query(1, ge=1, le=100_000)) -> Response:
    async with get_state(request).sessionmaker() as session:
        rows = await runs_store.list_runs(session, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)
    context = {"runs": rows[:PAGE_SIZE], "page": page, "has_next": len(rows) > PAGE_SIZE}
    return render(request, "runs.html", context)


@router.get("/runs/{run_id}")
async def run_detail(request: Request, run_id: int) -> Response:
    view = await _load_run(request, run_id)
    return render(request, "run_detail.html", view.context(request))


@router.get("/runs/{run_id}/transcripts/{key}")
async def transcripts(request: Request, run_id: int, key: str) -> Response:
    view = await _load_run(request, run_id, key)
    tasks_dir = get_state(request).config.tasks_dir
    names = await asyncio.to_thread(list_transcripts, tasks_dir, key)
    return render(
        request, "transcripts.html", {**view.context(request), "key": key, "names": names}
    )


@router.get("/runs/{run_id}/transcripts/{key}/{name}")
async def transcript(request: Request, run_id: int, key: str, name: str) -> Response:
    view = await _load_run(request, run_id, key)
    config = get_state(request).config
    result = await asyncio.to_thread(
        read_transcript, config.tasks_dir, key, name, config.secret_values()
    )
    if result is None:
        raise HTTPException(status_code=404)
    text, truncated = result
    context = {**view.context(request), "key": key, "name": name}
    return render(request, "transcript.html", {**context, "text": text, "truncated": truncated})
