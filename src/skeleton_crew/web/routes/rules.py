from functools import partial

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.domain.settings import MAX_RULES_CHARS, RULE_KEYS
from skeleton_crew.services import rules_service
from skeleton_crew.web.errors import Errors, on_invalid
from skeleton_crew.web.forms import form_value
from skeleton_crew.web.rendering import Message, redirect, render
from skeleton_crew.web.state import SessionDep

router = APIRouter()


async def _editor_page(
    request: Request,
    key: str,
    content: str | None,
    session: SessionDep,
    errors: Errors,
) -> Response:
    editor = await rules_service.editor(session, key)
    context = {
        "keys": RULE_KEYS,
        "key": key,
        "content": editor.content if content is None else content,
        "is_default": editor.is_default,
        "history": editor.history,
        "errors": errors,
        "max_chars": MAX_RULES_CHARS,
    }
    return render(request, "rules.html", context)


@router.get("/rules/{key}")
async def editor(request: Request, key: str, session: SessionDep) -> Response:
    return await _editor_page(request, key, None, session, {})


@router.post("/rules/{key}")
async def save(request: Request, key: str, session: SessionDep) -> Response:
    content = form_value(await request.form(), "content").replace("\r\n", "\n")
    on_invalid(request, partial(_editor_page, request, key, content))
    await rules_service.save_rules(session, key, content)
    return redirect(f"/rules/{key}", Message.SAVED)


@router.post("/rules/{key}/reset")
async def reset(key: str, session: SessionDep) -> Response:
    await rules_service.reset_rules(session, key)
    return redirect(f"/rules/{key}", Message.RULES_RESET)


@router.get("/rules/{key}/revisions/{revision_id}")
async def revision(request: Request, key: str, revision_id: int, session: SessionDep) -> Response:
    shown = await rules_service.revision(session, key, revision_id)
    return render(request, "rule_revision.html", {"keys": RULE_KEYS, "key": key, "revision": shown})
