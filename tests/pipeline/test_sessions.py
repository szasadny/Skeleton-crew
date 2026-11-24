import asyncio
from dataclasses import replace

import pytest
from fake_agent import limited_outcome, ok_outcome

from skeleton_crew.agents.base import failed_outcome
from skeleton_crew.domain.schemas import RefineResult
from skeleton_crew.pipeline.outcomes import (
    USAGE_LIMIT_REASON,
    Escalation,
    SessionFailed,
    Skip,
    UsageLimitReached,
)
from skeleton_crew.pipeline.sessions import AgentReply, require_result

RESULT = RefineResult(spec_markdown="Goal.")


def test_a_result_is_returned_and_the_run_keeps_going() -> None:
    halted = asyncio.Event()
    reply = AgentReply(ok_outcome("{}"), RESULT, None)
    assert require_result(reply, "refiner", halted) is RESULT
    assert not halted.is_set()


def test_a_missing_result_is_a_failed_session_that_escalates() -> None:
    halted = asyncio.Event()
    reply = AgentReply[RefineResult](failed_outcome("crashed"), None, "crashed (stopped early: x)")
    with pytest.raises(SessionFailed) as raised:
        require_result(reply, "review", halted)
    assert raised.value.reason == "review session failed: crashed (stopped early: x)"
    assert isinstance(raised.value, Escalation)
    assert not halted.is_set()


def test_the_usage_limit_halts_the_run_and_aborts_without_escalating() -> None:
    halted = asyncio.Event()
    reply = AgentReply[RefineResult](limited_outcome(), None, "session reported an error")
    with pytest.raises(UsageLimitReached) as raised:
        require_result(reply, "worker", halted)
    assert raised.value.reason == USAGE_LIMIT_REASON
    assert isinstance(raised.value, Skip)
    assert not isinstance(raised.value, Escalation)
    assert halted.is_set()


def test_a_valid_result_under_the_usage_limit_is_never_returned() -> None:
    halted = asyncio.Event()
    reply = AgentReply(replace(ok_outcome("{}"), usage_limited=True), RESULT, None)
    with pytest.raises(UsageLimitReached):
        require_result(reply, "worker", halted)
    assert halted.is_set()
