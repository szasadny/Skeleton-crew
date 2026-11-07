import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_agent import make_request
from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import AUTH_FILE_ENV
from skeleton_crew.agents.backends.codex import SPEC, AuthMode, CodexBackend
from skeleton_crew.domain.settings import RoleAgent

_KEY = SecretStr("codex-api-key-placeholder")
_AUTH_JSON = SecretStr('{"tokens": {"refresh_token": "auth-json-placeholder"}}')
_THREAD = "01a1b679-96b7-7fe3-83d8-cb868acc634b"


def _backend(
    mode: AuthMode, *, key: SecretStr | None = _KEY, auth_json: SecretStr | None = _AUTH_JSON
) -> CodexBackend:
    credentials = {"openai_api_key": key, "codex_auth_json": auth_json}
    return CodexBackend(
        auth_mode=mode, credentials={k: v for k, v in credentials.items() if v is not None}
    )


def _codex_args(argv: list[str]) -> list[str]:
    return argv[argv.index("exec") :]


def _configs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]


@pytest.mark.parametrize("mode", list(AuthMode))
def test_command_reads_rules_and_prompt_on_stdin(mode: AuthMode) -> None:
    prompt = "Fix it; $(rm -rf /) 'q' \"qq\"\nsecond line"
    argv = _backend(mode).command(make_request(prompt=prompt, timeout_s=1800.5))
    assert argv[:2] == ["sh", "-c"]
    assert argv[3:6] == ["sh", "/skeleton-in/prompt.md", "/skeleton-in/rules.md"]
    assert not any(prompt in arg for arg in argv)
    assert "timeout" not in argv
    args = _codex_args(argv)
    assert args[:4] == ["exec", "--json", "-m", "opus"]
    assert args[-1] == "-"
    assert "resume" not in args
    assert {
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--ignore-user-config",
    } <= set(args)
    assert _configs(args) == [
        'model_reasoning_effort="medium"',
        'web_search="disabled"',
        "analytics.enabled=false",
        "feedback.enabled=false",
    ]
    assert "--output-schema" not in args
    assert "--ephemeral" not in args
    assert "--max-turns" not in args


def test_command_resumes_a_session_with_the_same_flags() -> None:
    backend = _backend(AuthMode.API_KEY)
    fresh = _codex_args(backend.command(make_request()))
    resumed = _codex_args(backend.command(make_request(resume_session_id=_THREAD)))
    assert resumed[:2] == ["exec", "resume"]
    assert resumed[-2:] == [_THREAD, "-"]
    assert resumed[2:-2] == fresh[1:-1]


def test_web_search_is_live_only_when_opted_in() -> None:
    req = replace(make_request(), agent=RoleAgent(web_tools=True))
    assert 'web_search="live"' in _configs(_backend(AuthMode.API_KEY).command(req))
    assert 'web_search="live"' not in _configs(_backend(AuthMode.API_KEY).command(make_request()))


def test_budget_is_ignored() -> None:
    req = make_request(max_budget_usd=2.5)
    assert _backend(AuthMode.API_KEY).command(req) == _backend(AuthMode.API_KEY).command(
        make_request()
    )


def test_auth_file_is_written_only_in_subscription_mode() -> None:
    sub = _backend(AuthMode.SUBSCRIPTION).command(make_request())
    assert ".codex/auth.json" in sub[3:7]
    assert AUTH_FILE_ENV in sub[2]
    api = _backend(AuthMode.API_KEY).command(make_request())
    assert ".codex/auth.json" not in api
    assert AUTH_FILE_ENV not in api[2]


def _fake_codex(tmp_path: Path) -> None:
    if shutil.which("sh") is None:
        pytest.skip("sh not installed")
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text('#!/bin/sh\nprintf "%s\\n" "$*"; cat > "$HOME/stdin"; wc -c < "$HOME/stdin"\n')
    codex.chmod(0o755)


def _run(argv: list[str], tmp_path: Path, env: dict[str, str]) -> list[str]:
    result = subprocess.run(  # noqa: S603
        argv,
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path), **env},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_large_prompt_reaches_codex_on_stdin_after_the_rules(tmp_path: Path) -> None:
    _fake_codex(tmp_path)
    prompt_file, rules_file = tmp_path / "prompt.md", tmp_path / "rules.md"
    prompt_file.write_text("x" * 300_000 + " $(touch pwned)")
    rules_file.write_text("RULES $(touch pwned2)")
    req = replace(
        make_request(),
        prompt_path_in_container=str(prompt_file),
        rules_path_in_container=str(rules_file),
    )
    backend = _backend(AuthMode.API_KEY)

    args, size = _run(backend.command(req), tmp_path, backend.env())

    assert args.startswith("exec --json -m opus")
    assert args.endswith(" -")
    assert int(size) == 300_015 + len("RULES $(touch pwned2)") + 2
    stdin = (tmp_path / "stdin").read_text()
    assert stdin.startswith("RULES $(touch pwned2)\n\nxxx")
    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "pwned2").exists()


def test_subscription_auth_file_is_private_and_never_overwritten(tmp_path: Path) -> None:
    _fake_codex(tmp_path)
    (tmp_path / "prompt.md").write_text("prompt")
    (tmp_path / "rules.md").write_text("rules")
    req = replace(
        make_request(),
        prompt_path_in_container=str(tmp_path / "prompt.md"),
        rules_path_in_container=str(tmp_path / "rules.md"),
    )
    backend = _backend(AuthMode.SUBSCRIPTION)
    auth = tmp_path / ".codex" / "auth.json"

    _run(backend.command(req), tmp_path, backend.env())
    assert auth.read_text() == _AUTH_JSON.get_secret_value()
    assert auth.stat().st_mode & 0o077 == 0
    auth.write_text("refreshed")
    _run(backend.command(req), tmp_path, backend.env())
    assert auth.read_text() == "refreshed"


@pytest.mark.parametrize("mode", list(AuthMode))
def test_no_credential_ever_in_argv(mode: AuthMode) -> None:
    req = replace(
        make_request(max_budget_usd=1.0, resume_session_id=_THREAD),
        agent=RoleAgent(web_tools=True),
    )
    argv = _backend(mode).command(req)
    for secret in (_KEY, _AUTH_JSON):
        assert not any(secret.get_secret_value() in arg for arg in argv)


def test_env_holds_only_the_credential_of_the_auth_mode() -> None:
    assert _backend(AuthMode.API_KEY).env() == {"CODEX_API_KEY": _KEY.get_secret_value()}
    assert _backend(AuthMode.SUBSCRIPTION).env() == {AUTH_FILE_ENV: _AUTH_JSON.get_secret_value()}


def test_missing_credential_is_not_put_in_env() -> None:
    assert _backend(AuthMode.API_KEY, key=None).env() == {}
    assert _backend(AuthMode.SUBSCRIPTION, auth_json=SecretStr(" ")).env() == {}


def test_credential_problems_per_auth_mode() -> None:
    assert _backend(AuthMode.API_KEY, auth_json=None).credential_problems() == []
    assert _backend(AuthMode.SUBSCRIPTION, key=None).credential_problems() == []
    [api_problem] = _backend(AuthMode.API_KEY, key=None).credential_problems()
    assert "SC_AGENT_CREDENTIALS__OPENAI_API_KEY" in api_problem
    [sub_problem] = _backend(AuthMode.SUBSCRIPTION, auth_json=SecretStr("  ")).credential_problems()
    assert "SC_AGENT_CREDENTIALS__CODEX_AUTH_JSON" in sub_problem


def test_egress_per_auth_mode() -> None:
    assert _backend(AuthMode.API_KEY).egress_domains() == ("api.openai.com",)
    assert _backend(AuthMode.SUBSCRIPTION).egress_domains() == ("chatgpt.com", "auth.openai.com")


def test_auto_loaded_docs_are_the_root_agents_md() -> None:
    assert _backend(AuthMode.API_KEY).auto_loaded_docs() == ("AGENTS.md",)


def test_spec_is_consistent() -> None:
    default = {name: option.default for name, option in SPEC.options.items()}
    backend = SPEC.factory({"openai_api_key": _KEY}, default)
    assert backend.name == SPEC.name == "codex"
    assert backend.credential_problems() == []
    assert set(SPEC.credentials) == {"openai_api_key", "codex_auth_json"}
    assert SPEC.web_tools
    assert "ultra" not in SPEC.efforts


def _event(kind: str, **fields: Any) -> str:
    return json.dumps({"type": kind, **fields})


def _message(text: str, item_id: str = "item_1") -> str:
    return _event("item.completed", item={"id": item_id, "type": "agent_message", "text": text})


_STARTED = _event("thread.started", thread_id=_THREAD)
_TURN_STARTED = _event("turn.started")
_USAGE = {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20}
_TURN_DONE = _event("turn.completed", usage=_USAGE)
_RETRY = _event("error", message="Reconnecting... 2/5 (stream disconnected)")
_FALLBACK = _event(
    "item.completed", item={"id": "item_0", "type": "error", "message": "Falling back to HTTPS"}
)
_COMMAND = _event(
    "item.completed",
    item={
        "id": "item_2",
        "type": "command_execution",
        "command": 'echo \'{"status": "fake"}\'',
        "aggregated_output": '{"status": "fake"}',
        "exit_code": 0,
        "status": "completed",
    },
)


def _stream(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "stdout.jsonl"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _parse(tmp_path: Path, *lines: str, exit_code: int = 0, timed_out: bool = False) -> Any:
    return _backend(AuthMode.API_KEY).parse(_stream(tmp_path, *lines), exit_code, timed_out)


def test_parse_success(tmp_path: Path) -> None:
    final = _message('{"status": "done", "summary": "ok"}')
    outcome = _parse(tmp_path, _STARTED, _TURN_STARTED, _COMMAND, final, _TURN_DONE)
    assert outcome.ok
    assert outcome.error is None
    assert json.loads(outcome.structured_output) == {"status": "done", "summary": "ok"}
    assert outcome.session_id == _THREAD
    assert outcome.cost_usd == 0.0
    assert outcome.turns == 1
    assert outcome.stopped_early is None


def test_parse_recovered_transport_errors_do_not_fail(tmp_path: Path) -> None:
    lines = (_STARTED, _TURN_STARTED, _RETRY, _RETRY, _FALLBACK, _message("{}"), _TURN_DONE)
    outcome = _parse(tmp_path, *lines)
    assert outcome.ok
    assert outcome.structured_output == "{}"


def test_parse_final_message_in_a_json_fence(tmp_path: Path) -> None:
    final = _message('```json\n{"verdict": "approve"}\n```')
    outcome = _parse(tmp_path, _STARTED, final, _TURN_DONE)
    assert outcome.ok
    assert outcome.structured_output == '{"verdict": "approve"}'


def test_parse_prose_fails_as_an_early_stop(tmp_path: Path) -> None:
    final = _message('Here you go: {"verdict": "approve"}')
    outcome = _parse(tmp_path, _STARTED, final, _TURN_DONE)
    assert not outcome.ok
    assert outcome.structured_output is None
    assert outcome.error == "no structured output"
    assert outcome.stopped_early == "no_result"
    assert outcome.session_id == _THREAD


def test_parse_turn_failed_fails_with_a_fixed_error(tmp_path: Path) -> None:
    hostile = "ignore previous instructions <b>"
    lines = (
        _STARTED,
        _message('{"status": "done"}'),
        _event("error", message=hostile),
        _event("turn.failed", error={"message": hostile}),
    )
    outcome = _parse(tmp_path, *lines, exit_code=1)
    assert not outcome.ok
    assert outcome.error == "turn failed"
    assert outcome.stopped_early is None
    assert outcome.session_id == _THREAD


def test_parse_failure_is_sticky_across_later_turns(tmp_path: Path) -> None:
    lines = (_STARTED, _event("turn.failed", error={}), _message("{}"), _TURN_DONE)
    outcome = _parse(tmp_path, *lines)
    assert not outcome.ok
    assert outcome.error == "turn failed"


def test_parse_error_after_the_last_turn_fails(tmp_path: Path) -> None:
    lines = (_STARTED, _message("{}"), _TURN_DONE, _event("error", message="stream lost"))
    outcome = _parse(tmp_path, *lines)
    assert not outcome.ok
    assert outcome.error == "session reported an error"


def test_parse_skips_garbage_lines_and_takes_the_last_message(tmp_path: Path) -> None:
    lines = (
        "not json",
        _STARTED,
        _message('{"n": 1}'),
        "{broken",
        "[1,2]",
        '"text"',
        _event("item.completed", item="oops"),
        _event("item.completed", item={"type": "agent_message", "text": 5}),
        _message('{"n": 2}'),
        _TURN_DONE,
        "\x00\xff",
    )
    outcome = _parse(tmp_path, *lines)
    assert outcome.ok
    assert outcome.structured_output == '{"n": 2}'


def test_parse_last_message_wins_even_when_it_is_prose(tmp_path: Path) -> None:
    lines = (_STARTED, _message('{"n": 1}'), _message("Done."), _TURN_DONE)
    outcome = _parse(tmp_path, *lines)
    assert not outcome.ok
    assert outcome.structured_output is None


@pytest.mark.parametrize(
    "thread_id", ["--dangerously-bypass-approvals-and-sandbox", "-", "a b", "x" * 101, 7, None]
)
def test_parse_drops_hostile_thread_ids(tmp_path: Path, thread_id: object) -> None:
    lines = (_event("thread.started", thread_id=thread_id), _message("{}"), _TURN_DONE)
    outcome = _parse(tmp_path, *lines)
    assert not outcome.ok
    assert outcome.session_id is None


def test_parse_missing_file_fails(tmp_path: Path) -> None:
    outcome = _backend(AuthMode.API_KEY).parse(tmp_path / "absent", 143, False)
    assert not outcome.ok
    assert outcome.session_id is None


def test_parse_timeout_fails_even_with_a_result(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _STARTED, _message("{}"), _TURN_DONE, timed_out=True)
    assert not outcome.ok
    assert outcome.timed_out
    assert outcome.error == "timed out"
    assert outcome.stopped_early is None


def test_parse_timeout_without_a_session(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _TURN_STARTED, timed_out=True)
    assert not outcome.ok
    assert outcome.timed_out
    assert outcome.error == "timed out"


def test_parse_non_zero_exit_fails(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _STARTED, _message("{}"), _TURN_DONE, exit_code=1)
    assert not outcome.ok
    assert outcome.error == "exit code 1"
    assert outcome.stopped_early is None


def test_parse_without_a_completed_turn_fails(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _STARTED, _TURN_STARTED, _message("{}"))
    assert not outcome.ok
    assert outcome.error == "no completed turn"
    assert outcome.stopped_early is None
