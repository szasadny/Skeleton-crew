import asyncio
from functools import partial

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.store import runs_store
from skeleton_crew.web.errors import found
from skeleton_crew.web.pagination import PageParam, paginate
from skeleton_crew.web.rendering import render
from skeleton_crew.web.state import SessionDep, get_state
from skeleton_crew.web.transcripts import MAX_TRANSCRIPT_BYTES, list_transcripts, read_transcript
from skeleton_crew.web.views.runs import load_run

router = APIRouter()

_BYTES_PER_KB = 1024


@router.get("/runs")
async def runs(request: Request, session: SessionDep, page: PageParam = 1) -> Response:
    rows = await paginate(partial(runs_store.runs_with_blocked_counts, session), page)
    return render(request, "runs.html", {"page": rows})


@router.get("/runs/{run_id}")
async def run_detail(request: Request, run_id: int, session: SessionDep) -> Response:
    view = await load_run(session, run_id)
    return render(request, "run_detail.html", view.context())


@router.get("/runs/{run_id}/transcripts/{key}")
async def transcripts(request: Request, run_id: int, key: str, session: SessionDep) -> Response:
    view = await load_run(session, run_id, key)
    names = await asyncio.to_thread(list_transcripts, get_state(request).config.tasks_dir, key)
    return render(request, "transcripts.html", {**view.context(), "key": key, "names": names})


@router.get("/runs/{run_id}/transcripts/{key}/{name}")
async def transcript(
    request: Request, run_id: int, key: str, name: str, session: SessionDep
) -> Response:
    view = await load_run(session, run_id, key)
    config = get_state(request).config
    text, truncated = found(
        await asyncio.to_thread(
            read_transcript, config.tasks_dir, key, name, config.secret_values()
        )
    )
    context = {
        **view.context(),
        "key": key,
        "name": name,
        "text": text,
        "truncated": truncated,
        "max_kb": MAX_TRANSCRIPT_BYTES // _BYTES_PER_KB,
    }
    return render(request, "transcript.html", context)
