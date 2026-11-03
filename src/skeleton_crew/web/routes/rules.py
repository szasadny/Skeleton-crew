from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from skeleton_crew.agents.rules import default_rules
from skeleton_crew.domain.settings import RULE_KEYS
from skeleton_crew.store import rules_store
from skeleton_crew.web.rendering import redirect, render
from skeleton_crew.web.state import get_state

router = APIRouter()

MAX_RULES_CHARS = 20_000
HISTORY_LIMIT = 20


def _check_key(key: str) -> None:
    if key not in RULE_KEYS:
        raise HTTPException(status_code=404)


async def _render_editor(
    request: Request, key: str, content: str | None = None, error: str | None = None
) -> Response:
    async with get_state(request).sessionmaker() as session:
        active = await rules_store.active_rules(session, key)
        history = await rules_store.rule_history(session, key, HISTORY_LIMIT)
    context = {
        "keys": RULE_KEYS,
        "key": key,
        "content": content if content is not None else active or default_rules(key),
        "is_default": active is None,
        "history": history,
        "error": error,
        "max_chars": MAX_RULES_CHARS,
    }
    return render(request, "rules.html", context, 422 if error else 200)


@router.get("/rules/{key}")
async def editor(request: Request, key: str) -> Response:
    _check_key(key)
    return await _render_editor(request, key)


async def _save(request: Request, key: str, content: str) -> None:
    async with get_state(request).sessionmaker() as session:
        await rules_store.save_rules(session, key, content)
        await session.commit()


@router.post("/rules/{key}")
async def save(request: Request, key: str) -> Response:
    _check_key(key)
    raw = (await request.form()).get("content")
    content = raw.replace("\r\n", "\n") if isinstance(raw, str) else ""
    if not content.strip():
        return await _render_editor(request, key, content, "Rules cannot be empty.")
    if len(content) > MAX_RULES_CHARS:
        error = f"Rules are limited to {MAX_RULES_CHARS} characters."
        return await _render_editor(request, key, content, error)
    await _save(request, key, content)
    return redirect(f"/rules/{key}", "saved")


@router.post("/rules/{key}/reset")
async def reset(request: Request, key: str) -> Response:
    _check_key(key)
    await _save(request, key, default_rules(key))
    return redirect(f"/rules/{key}", "rules_reset")


@router.get("/rules/{key}/revisions/{revision_id}")
async def revision(request: Request, key: str, revision_id: int) -> Response:
    _check_key(key)
    async with get_state(request).sessionmaker() as session:
        history = await rules_store.rule_history(session, key, HISTORY_LIMIT)
    match = next((rev for rev in history if rev.id == revision_id), None)
    if match is None:
        raise HTTPException(status_code=404)
    return render(request, "rule_revision.html", {"keys": RULE_KEYS, "key": key, "revision": match})
