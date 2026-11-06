from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from skeleton_crew.domain.settings import SelfHostedCi
from skeleton_crew.web.forms import Kind, SettingsPage, number_specs
from skeleton_crew.web.state import get_state

router = APIRouter()

_NUMBERS = number_specs("ci", SelfHostedCi, ["runners", "cpus", "memory_gb", "pids"])


def _live(request: Request) -> Mapping[str, Any]:
    state = get_state(request)
    return {
        "runners": state.runners.statuses(),
        "repo_url": f"https://github.com/{state.config.repo}",
        "runners_dir": state.config.runners_dir,
        "runner_image": state.config.runner_image,
    }


_PAGE = SettingsPage(
    template="ci.html",
    fields={
        "ci.enabled": Kind.BOOL,
        "ci.labels": Kind.LINES,
        "ci.egress_allowlist": Kind.LINES,
        "ci.no_proxy": Kind.LINES,
        **{spec.name: Kind.NUMBER for spec in _NUMBERS},
    },
    context={"numbers": _NUMBERS},
    live_context=_live,
)


@router.get("/ci")
async def show(request: Request) -> Response:
    return await _PAGE.show(request)


@router.post("/ci")
async def submit(request: Request) -> Response:
    return await _PAGE.submit(request)
