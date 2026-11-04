import re
from collections.abc import Mapping
from enum import StrEnum
from functools import cache

from jinja2 import PackageLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from skeleton_crew.domain.settings import Role


class PromptName(StrEnum):
    SELECTOR = "selector"
    REFINER = "refiner"
    WORKER = "worker"
    WORKER_FOLLOWUP = "worker_followup"
    WORKER_UPDATE = "worker_update"
    REVIEWER = "reviewer"
    REVIEWER_FOLLOWUP = "reviewer_followup"
    CONTINUE = "continue"


_DELIMITER_RE = re.compile(r"<(\s*/?\s*untrusted)", re.IGNORECASE)
_LABEL_RE = re.compile(r"^[A-Za-z0-9 #._\[\]-]{1,80}$")


def untrusted(label: str, text: object) -> str:
    if not _LABEL_RE.fullmatch(label):
        raise ValueError(f"invalid untrusted block label: {label!r}")
    body = _DELIMITER_RE.sub(r"&lt;\1", str(text))
    return f'<untrusted label="{label}">\n{body}\n</untrusted>'


def _untrusted_filter(text: object, label: str) -> str:
    return untrusted(label, text)


@cache
def _environment() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        loader=PackageLoader("skeleton_crew.agents", "prompts"),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["untrusted"] = _untrusted_filter
    return env


def render_prompt(
    name: Role | PromptName, context: Mapping[str, object], *, json_schema: str
) -> str:
    template = _environment().get_template(f"{PromptName(name.value)}.md.j2")
    return template.render({**context, "json_schema": json_schema})
