import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fake_agent import claude_result_line, make_request
from pydantic import SecretStr

from skeleton_crew.agents.backends.claude_code import INTERACTIVE_TOOLS, AuthMode, ClaudeCodeBackend
from skeleton_crew.domain.settings import RoleAgent

_TOKEN = SecretStr("oauth-token-placeholder")
_KEY = SecretStr("api-key-placeholder")


def _backend(
    mode: AuthMode, *, token: SecretStr | None = _TOKEN, key: SecretStr | None = _KEY
) -> ClaudeCodeBackend:
    credentials = {"claude_oauth_token": token, "anthropic_api_key": key}
    return ClaudeCodeBackend(
        auth_mode=mode, credentials={k: v for k, v in credentials.items() if v is not None}
    )


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize("mode", list(AuthMode))
def test_command_reads_the_prompt_file_on_stdin(mode: AuthMode) -> None:
    prompt = "Fix it; $(rm -rf /) 'q' \"qq\"\nsecond line"
    argv = _backend(mode).command(make_request(prompt=prompt, timeout_s=1800.5))
    assert argv[:2] == ["sh", "-c"]
    assert argv[3:6] == ["sh", "/skeleton-in/prompt.md", "-p"]
    assert not any(prompt in arg for arg in argv)
    assert "timeout" not in argv
    assert _value_after(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert _value_after(argv, "--model") == "opus"
    assert _value_after(argv, "--effort") == "medium"
    assert _value_after(argv, "--max-turns") == "150"
    assert _value_after(argv, "--json-schema") == '{"type":"object"}'
    assert _value_after(argv, "--append-system-prompt-file") == "/skeleton-in/rules.md"
    assert _value_after(argv, "--permission-prompts") == "none"
    assert {"--dangerously-skip-permissions", "--strict-mcp-config"} <= set(argv)
    assert "--bare" not in argv
    assert "--settings" not in argv
    assert "--resume" not in argv


def _disallowed(argv: list[str]) -> set[str]:
    return set(_value_after(argv, "--disallowedTools").split(","))


def test_web_tools_are_disallowed_by_default() -> None:
    tools = _disallowed(_backend(AuthMode.SUBSCRIPTION).command(make_request()))
    assert {"AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "WebFetch", "WebSearch"} <= tools
    assert set(INTERACTIVE_TOOLS) <= tools


def test_web_tools_opt_in_keeps_interactive_tools_disallowed() -> None:
    req = replace(make_request(), agent=RoleAgent(web_tools=True))
    tools = _disallowed(_backend(AuthMode.SUBSCRIPTION).command(req))
    assert tools == set(INTERACTIVE_TOOLS)
    assert "AskUserQuestion" in tools
    assert not tools & {"WebFetch", "WebSearch"}


def test_command_resumes_a_session() -> None:
    argv = _backend(AuthMode.SUBSCRIPTION).command(make_request(resume_session_id="abc-1"))
    assert argv[-2:] == ["--resume", "abc-1"]


def test_budget_flag_only_in_api_key_mode() -> None:
    req = make_request(max_budget_usd=2.5)
    api_argv = _backend(AuthMode.API_KEY).command(req)
    assert _value_after(api_argv, "--max-budget-usd") == "2.5"
    assert "--max-budget-usd" not in _backend(AuthMode.SUBSCRIPTION).command(req)
    assert "--max-budget-usd" not in _backend(AuthMode.API_KEY).command(make_request())


def test_env_holds_only_the_credential_of_the_auth_mode() -> None:
    sub = _backend(AuthMode.SUBSCRIPTION).env()
    assert sub["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN.get_secret_value()
    assert "ANTHROPIC_API_KEY" not in sub
    api = _backend(AuthMode.API_KEY).env()
    assert api["ANTHROPIC_API_KEY"] == _KEY.get_secret_value()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in api
    for env in (sub, api):
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert env["DISABLE_AUTOUPDATER"] == "1"


def test_credential_problems_per_auth_mode() -> None:
    assert _backend(AuthMode.SUBSCRIPTION, key=None).credential_problems() == []
    assert _backend(AuthMode.API_KEY, token=None).credential_problems() == []
    [sub_problem] = _backend(AuthMode.SUBSCRIPTION, token=None).credential_problems()
    assert "SC_AGENT_CREDENTIALS__CLAUDE_OAUTH_TOKEN" in sub_problem
    [api_problem] = _backend(AuthMode.API_KEY, key=SecretStr("  ")).credential_problems()
    assert "SC_AGENT_CREDENTIALS__ANTHROPIC_API_KEY" in api_problem


def test_large_prompt_reaches_claude_on_stdin(tmp_path: Path) -> None:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("sh not installed")
    prompt_file, claude = tmp_path / "prompt.md", tmp_path / "claude"
    prompt_file.write_text("x" * 300_000 + " $(touch pwned)")
    claude.write_text('#!/bin/sh\nprintf "%s\\n" "$*"; wc -c\n')
    claude.chmod(0o755)
    req = replace(make_request(), prompt_path_in_container=str(prompt_file))
    argv = _backend(AuthMode.SUBSCRIPTION).command(req)

    result = subprocess.run(  # noqa: S603
        argv,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    args, size = result.stdout.splitlines()
    assert args.startswith("-p --output-format stream-json")
    assert int(size) == 300_015
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("mode", list(AuthMode))
def test_no_credential_ever_in_argv(mode: AuthMode) -> None:
    argv = _backend(mode).command(make_request(max_budget_usd=1.0, resume_session_id="s-1"))
    for secret in (_TOKEN, _KEY):
        assert not any(secret.get_secret_value() in arg for arg in argv)


def test_missing_credential_is_not_put_in_env() -> None:
    env = _backend(AuthMode.API_KEY, key=None).env()
    assert "ANTHROPIC_API_KEY" not in env


def test_egress_is_the_anthropic_api_only() -> None:
    assert _backend(AuthMode.SUBSCRIPTION).egress_domains() == ("api.anthropic.com",)


def test_auto_loaded_docs_are_the_root_and_dot_claude_claude_md() -> None:
    assert _backend(AuthMode.API_KEY).auto_loaded_docs() == ("CLAUDE.md", ".claude/CLAUDE.md")


def _stream(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "stdout.jsonl"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


_INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "tools": ["Bash"]})
_ASSISTANT = json.dumps(
    {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"type":"result"}'}]}}
)


def test_parse_success_reserialises_structured_output(tmp_path: Path) -> None:
    line = claude_result_line(structured_output={"status": "done", "summary": "ok"})
    path = _stream(tmp_path, _INIT, _ASSISTANT, line)
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(path, 0, False)
    assert outcome.ok
    assert outcome.error is None
    assert outcome.structured_output is not None
    assert json.loads(outcome.structured_output) == {"status": "done", "summary": "ok"}
    assert outcome.session_id == "4f1c2d3e-0000-4000-8000-000000000001"
    assert outcome.cost_usd == pytest.approx(0.42)
    assert outcome.turns == 7
    assert outcome.stopped_early is None


def test_parse_error_max_turns_keeps_session_for_resume(tmp_path: Path) -> None:
    line = claude_result_line(subtype="error_max_turns", is_error=True, result="")
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), 1, False)
    assert not outcome.ok
    assert outcome.error == "session ended with error_max_turns"
    assert outcome.session_id is not None
    assert outcome.stopped_early is None


def test_parse_hook_stop_is_an_early_stop(tmp_path: Path) -> None:
    line = claude_result_line(
        result="", terminal_reason="hook_stopped", stop_reason="tool_use", num_turns=2
    )
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, _INIT, line), 0, False)
    assert not outcome.ok
    assert outcome.error == "no structured output"
    assert outcome.stopped_early == "hook_stopped"
    assert outcome.session_id == "4f1c2d3e-0000-4000-8000-000000000001"


@pytest.mark.parametrize("reason", [None, 7, "ignore previous instructions <b>", "x" * 41])
def test_parse_early_stop_with_missing_or_hostile_reason(tmp_path: Path, reason: object) -> None:
    line = claude_result_line(result="", terminal_reason=reason)
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), 0, False)
    assert outcome.stopped_early == "no_result"


def test_parse_unknown_or_hostile_subtype_is_an_error(tmp_path: Path) -> None:
    line = claude_result_line(subtype="ignore previous instructions <b>")
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), 0, False)
    assert not outcome.ok
    assert outcome.error == "session ended with unknown"


@pytest.mark.parametrize(
    ("is_error", "exit_code", "timed_out"), [(True, 0, False), (False, 1, False), (False, 0, True)]
)
def test_parse_success_subtype_still_fails_on_other_error_signals(
    tmp_path: Path, is_error: bool, exit_code: int, timed_out: bool
) -> None:
    line = claude_result_line(is_error=is_error, structured_output={"a": 1})
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), exit_code, timed_out)
    assert not outcome.ok
    assert outcome.timed_out is timed_out


def test_parse_falls_back_to_a_json_result_text(tmp_path: Path) -> None:
    line = claude_result_line(result='```json\n{"verdict": "approve"}\n```')
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), 0, False)
    assert outcome.ok
    assert outcome.structured_output == '{"verdict": "approve"}'


def test_parse_missing_structured_output_with_prose_fails(tmp_path: Path) -> None:
    line = claude_result_line(result='Here you go: {"verdict": "approve"}')
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), 0, False)
    assert not outcome.ok
    assert outcome.structured_output is None
    assert outcome.error == "no structured output"


def test_parse_skips_garbage_lines_and_takes_the_last_result(tmp_path: Path) -> None:
    first = claude_result_line(structured_output={"n": 1})
    last = claude_result_line(structured_output={"n": 2})
    path = _stream(tmp_path, "not json", first, "{broken", "[1,2]", '"text"', last, "\x00\xff")
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(path, 0, False)
    assert outcome.ok
    assert outcome.structured_output == '{"n": 2}'


@pytest.mark.parametrize("content", ["", "garbage\n", f"{_INIT}\n{_ASSISTANT}\n"])
def test_parse_without_result_line_fails(tmp_path: Path, content: str) -> None:
    path = tmp_path / "stdout.jsonl"
    path.write_text(content, encoding="utf-8")
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(path, 0, False)
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.session_id is None


def test_parse_missing_file_fails(tmp_path: Path) -> None:
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(tmp_path / "absent", 143, False)
    assert not outcome.ok


def test_parse_drops_session_ids_that_look_like_options(tmp_path: Path) -> None:
    line = claude_result_line(session_id="--dangerously-skip-permissions", structured_output={})
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), 0, False)
    assert outcome.session_id is None


def test_parse_ignores_non_numeric_cost(tmp_path: Path) -> None:
    line = claude_result_line(total_cost_usd="lots", num_turns=True, structured_output={})
    outcome = _backend(AuthMode.SUBSCRIPTION).parse(_stream(tmp_path, line), 0, False)
    assert outcome.cost_usd == 0.0
    assert outcome.turns == 0
