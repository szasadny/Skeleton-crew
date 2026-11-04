import logging
from pathlib import Path

import pytest
from fake_agent import ExecStep, FakeBackend, FakeExecutor, claude_result_line, make_request
from pydantic import SecretStr

from skeleton_crew.agents.backends.claude_code import AuthMode, ClaudeCodeBackend
from skeleton_crew.agents.session import run_session
from skeleton_crew.domain.settings import Role

_TOKEN = "oauth-token-placeholder-1234"  # noqa: S105


def _claude() -> ClaudeCodeBackend:
    return ClaudeCodeBackend(
        auth_mode=AuthMode.SUBSCRIPTION, credentials={"claude_oauth_token": SecretStr(_TOKEN)}
    )


async def test_successful_session_returns_structured_output(tmp_path: Path) -> None:
    stdout = claude_result_line(structured_output={"status": "done"}) + "\n"
    executor = FakeExecutor(default=ExecStep(stdout=stdout))
    outcome = await run_session(_claude(), executor, make_request(), tmp_path / "t" / "w.jsonl")
    assert outcome.ok
    assert outcome.structured_output == '{"status": "done"}'
    [call] = executor.calls
    assert call.argv[:2] == ["claude", "-p"]
    assert call.env["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN
    assert call.cwd == "/workspace"
    assert call.timeout_s == make_request().timeout_s


async def test_transcript_is_rewritten_without_credentials(tmp_path: Path) -> None:
    leaked = claude_result_line(structured_output={"summary": f"token {_TOKEN}"})
    executor = FakeExecutor(default=ExecStep(stdout=f'{{"echo": "{_TOKEN}"}}\n{leaked}\n'))
    transcript = tmp_path / "w.jsonl"
    outcome = await run_session(_claude(), executor, make_request(), transcript)
    text = transcript.read_text(encoding="utf-8")
    assert _TOKEN not in text
    assert "***" in text
    assert outcome.structured_output is not None
    assert _TOKEN not in outcome.structured_output


@pytest.mark.parametrize(
    "step",
    [
        ExecStep(timed_out=True, exit_code=-1),
        ExecStep(timed_out=True, exit_code=124, stdout="partial\n"),
    ],
)
async def test_timeout_returns_a_failed_outcome(tmp_path: Path, step: ExecStep) -> None:
    executor = FakeExecutor(default=step)
    outcome = await run_session(_claude(), executor, make_request(), tmp_path / "w.jsonl")
    assert not outcome.ok
    assert outcome.timed_out
    assert outcome.error == "timed out"


async def test_executor_failure_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    executor = FakeExecutor(default=ExecStep(stdout=_TOKEN, error=RuntimeError("docker gone")))
    transcript = tmp_path / "w.jsonl"
    with caplog.at_level(logging.ERROR):
        outcome = await run_session(_claude(), executor, make_request(), transcript)
    assert not outcome.ok
    assert outcome.error == "executor failed: RuntimeError"
    assert _TOKEN not in transcript.read_text(encoding="utf-8")


async def test_stderr_is_logged_redacted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    executor = FakeExecutor(default=ExecStep(exit_code=1, stderr_tail=f"auth {_TOKEN} failed"))
    with caplog.at_level(logging.WARNING):
        outcome = await run_session(_claude(), executor, make_request(), tmp_path / "w.jsonl")
    assert not outcome.ok
    assert "auth *** failed" in caplog.text
    assert _TOKEN not in caplog.text


async def test_fake_backend_replays_scripted_outcomes_per_role(tmp_path: Path) -> None:
    backend = FakeBackend({Role.WORKER: ['{"n": 1}', '{"n": 2}'], Role.REVIEWER: ['{"r": 1}']})
    executor = FakeExecutor()
    worker, reviewer = make_request(Role.WORKER), make_request(Role.REVIEWER)
    first = await run_session(backend, executor, worker, tmp_path / "a")
    review = await run_session(backend, executor, reviewer, tmp_path / "b")
    second = await run_session(backend, executor, worker, tmp_path / "c")
    exhausted = await run_session(backend, executor, worker, tmp_path / "d")
    assert [first.structured_output, review.structured_output, second.structured_output] == [
        '{"n": 1}',
        '{"r": 1}',
        '{"n": 2}',
    ]
    assert not exhausted.ok
    assert backend.requests == [worker, reviewer, worker, worker]
