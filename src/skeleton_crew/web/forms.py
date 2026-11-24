from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import NoneType
from typing import Any, get_args

from pydantic import BaseModel
from starlette.datastructures import ImmutableMultiDict, UploadFile

from skeleton_crew.domain.settings import AppSettings

_ACRONYMS = {"usd": "USD", "ci": "CI", "gb": "GB"}

type FormFields = ImmutableMultiDict[str, UploadFile | str]


class Kind(StrEnum):
    TEXT = "text"
    OPTIONAL_TEXT = "optional_text"
    NUMBER = "number"
    OPTIONAL_NUMBER = "optional_number"
    BOOL = "bool"
    LINES = "lines"
    MULTI = "multi"


def label(name: str) -> str:
    words = " ".join(_ACRONYMS.get(word, word) for word in name.split("_"))
    return words[:1].upper() + words[1:]


def form_value(form: FormFields, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def parse_form(form: FormFields, fields: Mapping[str, Kind]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for name, kind in fields.items():
        text = form_value(form, name).strip()
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


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


def form_values(settings: AppSettings) -> dict[str, object]:
    return _flatten(settings.model_dump(mode="json"))


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
