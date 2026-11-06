from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import NoneType
from typing import Any, get_args

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData

from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.store.settings_store import load_settings, save_settings
from skeleton_crew.web.rendering import redirect, render
from skeleton_crew.web.state import get_state

GENERAL = ""
_ACRONYMS = {"usd": "USD", "ci": "CI", "gb": "GB"}


class Kind(StrEnum):
    TEXT = "text"
    OPTIONAL_TEXT = "optional_text"
    NUMBER = "number"
    OPTIONAL_NUMBER = "optional_number"
    BOOL = "bool"
    LINES = "lines"
    MULTI = "multi"


def label(name: str) -> str:
    words = [_ACRONYMS.get(word, word) for word in name.split("_")]
    return " ".join(words)[:1].upper() + " ".join(words)[1:]


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def parse_form(form: FormData, fields: Mapping[str, Kind]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for name, kind in fields.items():
        text = _text(form, name)
        match kind:
            case Kind.TEXT | Kind.NUMBER:
                parsed[name] = text
            case Kind.OPTIONAL_TEXT | Kind.OPTIONAL_NUMBER:
                parsed[name] = text or None
            case Kind.BOOL:
                parsed[name] = name in form
            case Kind.LINES:
                parsed[name] = [line.strip() for line in text.splitlines() if line.strip()]
            case Kind.MULTI:
                parsed[name] = [v for v in form.getlist(name) if isinstance(v, str)]
    return parsed


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


def merge(current: AppSettings, updates: Mapping[str, object]) -> dict[str, Any]:
    data: dict[str, Any] = current.model_dump(mode="json")
    for dotted, value in updates.items():
        *parents, leaf = dotted.split(".")
        target = data
        for part in parents:
            target = target.setdefault(part, {})
        target[leaf] = value
    return data


def _message(error: Mapping[str, Any]) -> str:
    return str(error["msg"]).removeprefix("Value error, ")


def field_errors(exc: ValidationError, known: Iterable[str]) -> dict[str, list[str]]:
    names = set(known)
    errors: dict[str, list[str]] = {}
    for error in exc.errors():
        parts = [str(part) for part in error["loc"]]
        prefixes = (".".join(parts[:i]) for i in range(len(parts), 0, -1))
        target = next((p for p in prefixes if p in names), GENERAL)
        errors.setdefault(target, []).append(_message(error))
    return errors


@dataclass(frozen=True)
class NumberSpec:
    name: str
    label: str
    min: float | None
    max: float | None
    step: str
    optional: bool


def number_specs(prefix: str, model: type[BaseModel], names: Iterable[str]) -> list[NumberSpec]:
    specs = []
    for name in names:
        info = model.model_fields[name]
        args = [arg for arg in get_args(info.annotation) if arg is not NoneType]
        number_type = args[0] if args else info.annotation
        low = high = None
        for meta in info.metadata:
            low = getattr(meta, "ge", getattr(meta, "gt", low))
            high = getattr(meta, "le", getattr(meta, "lt", high))
        specs.append(
            NumberSpec(
                name=f"{prefix}.{name}",
                label=label(name),
                min=low,
                max=high,
                step="1" if number_type is int else "any",
                optional=NoneType in get_args(info.annotation),
            )
        )
    return specs


@dataclass(frozen=True)
class SettingsPage:
    template: str
    fields: Mapping[str, Kind]
    context: Mapping[str, Any] = field(default_factory=dict)
    on_saved: Callable[[Request, AppSettings], None] | None = None
    check: Callable[[AppSettings], dict[str, list[str]]] | None = None
    live_context: Callable[[Request], Mapping[str, Any]] | None = None

    def _render(
        self,
        request: Request,
        values: Mapping[str, object],
        errors: Mapping[str, list[str]],
        status_code: int = 200,
    ) -> Response:
        live = self.live_context(request) if self.live_context is not None else {}
        context = {**self.context, **live, "values": values, "errors": errors}
        return render(request, self.template, context, status_code)

    async def show(self, request: Request) -> Response:
        async with get_state(request).sessionmaker() as session:
            current = await load_settings(session)
        return self._render(request, flatten(current.model_dump(mode="json")), {})

    async def submit(self, request: Request) -> Response:
        updates = parse_form(await request.form(), self.fields)
        async with get_state(request).sessionmaker() as session:
            current = await load_settings(session)
            try:
                new = AppSettings.model_validate(merge(current, updates))
            except ValidationError as exc:
                errors = field_errors(exc, self.fields)
            else:
                errors = self.check(new) if self.check is not None else {}
            if errors:
                values = flatten(current.model_dump(mode="json")) | updates
                return self._render(request, values, errors, 422)
            await save_settings(session, new)
            await session.commit()
        if self.on_saved is not None:
            self.on_saved(request, new)
        return redirect(request.url.path, "saved")
