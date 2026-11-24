from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.services import settings_service
from skeleton_crew.services.settings_service import Check
from skeleton_crew.store.settings_store import load_settings
from skeleton_crew.web.errors import Errors, on_invalid
from skeleton_crew.web.forms import Kind, form_values, parse_form
from skeleton_crew.web.rendering import Message, redirect, render
from skeleton_crew.web.state import SessionDep, WebState, get_state

type LiveContext = Callable[[WebState, Mapping[str, object]], Mapping[str, Any]]
type OnSaved = Callable[[WebState, AppSettings], None]


@dataclass(frozen=True)
class PageHooks:
    check: Check | None = None
    on_saved: OnSaved | None = None
    live_context: LiveContext | None = None


@dataclass(frozen=True)
class SettingsPage:
    template: str
    fields: Mapping[str, Kind]
    context: Mapping[str, Any] = field(default_factory=dict)
    hooks: PageHooks = PageHooks()

    def include(self, router: APIRouter, path: str, *, draft: bool = False) -> None:
        router.add_api_route(path, self.show, methods=["GET"])
        router.add_api_route(path, self.submit, methods=["POST"])
        if draft:
            router.add_api_route(f"{path}/draft", self.draft, methods=["GET"])

    async def _render(
        self,
        request: Request,
        updates: Mapping[str, object],
        session: SessionDep,
        errors: Errors,
    ) -> Response:
        values = form_values(await load_settings(session)) | dict(updates)
        live = self.hooks.live_context
        extra = live(get_state(request), values) if live is not None else {}
        context = {**self.context, **extra, "values": values, "errors": errors}
        return render(request, self.template, context)

    def _on_saved(self, request: Request) -> settings_service.OnSaved | None:
        hook = self.hooks.on_saved
        return partial(hook, get_state(request)) if hook is not None else None

    async def show(self, request: Request, session: SessionDep) -> Response:
        return await self._render(request, {}, session, {})

    async def draft(self, request: Request, session: SessionDep) -> Response:
        return await self._render(
            request, parse_form(request.query_params, self.fields), session, {}
        )

    async def submit(self, request: Request, session: SessionDep) -> Response:
        updates = parse_form(await request.form(), self.fields)
        on_invalid(request, partial(self._render, request, updates))
        await settings_service.update_settings(
            session, updates, self.hooks.check, self._on_saved(request)
        )
        return redirect(request.url.path, Message.SAVED)
