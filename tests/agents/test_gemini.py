import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_agent import make_request
from fake_cli import (
    HOSTILE_PROMPT,
    LARGE_PROMPT,
    credentials,
    fake_cli,
    pwned,
    run_argv,
    value_after,
    write_jsonl,
)
from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import AUTH_FILE_ENV, AuthMode
from skeleton_crew.agents.backends.gemini import (
    INTERACTIVE_TOOLS,
    SETTINGS_FILE,
    WEB_TOOLS,
    GeminiBackend,
)
from skeleton_crew.agents.base import SessionOutcome, SessionRequest
from skeleton_crew.domain.settings import RoleAgent

_KEY = SecretStr("api-key-placeholder")
_CREDS = SecretStr('{"refresh_token": "oauth-placeholder"}')
_SESSION = "8cc0c750-3dd2-45ba-9b04-418d6f2495bc"


def _backend(
    mode: AuthMode, *, key: SecretStr | None = _KEY, creds: SecretStr | None = _CREDS
) -> GeminiBackend:
    return GeminiBackend(
        auth_mode=mode, credentials=credentials(gemini_api_key=key, gemini_oauth_creds_json=creds)
    )


def _settings(argv: list[str]) -> dict[str, Any]:
    settings: dict[str, Any] = json.loads(value_after(argv, SETTINGS_FILE))
    return settings


def _excluded(argv: list[str]) -> set[str]:
    return set(_settings(argv)["tools"]["exclude"])


@pytest.mark.parametrize("mode", list(AuthMode))
def test_command_pipes_rules_and_prompt_on_stdin(mode: AuthMode) -> None:
    argv = _backend(mode).command(make_request(prompt=HOSTILE_PROMPT, timeout_s=1800.5))
    assert argv[:2] == ["sh", "-c"]
    assert argv[3:6] == ["sh", "/skeleton-in/prompt.md", "/skeleton-in/rules.md"]
    assert not any(HOSTILE_PROMPT in arg for arg in argv)
    assert "timeout" not in argv
    assert "-p" not in argv
    assert value_after(argv, "--output-format") == "stream-json"
    assert value_after(argv, "--model") == "opus"
    assert value_after(argv, "--approval-mode") == "yolo"
    assert "--skip-trust" in argv
    assert value_after(argv, "--allowed-mcp-server-names") == "skeleton-crew-no-mcp"
    assert "--resume" not in argv
    assert not any("effort" in arg or "budget" in arg for arg in argv)


def test_auth_type_and_oauth_file_follow_the_mode() -> None:
    api = _backend(AuthMode.API_KEY).command(make_request())
    sub = _backend(AuthMode.SUBSCRIPTION).command(make_request())
    assert _settings(api)["security"]["auth"]["selectedType"] == "gemini-api-key"
    assert _settings(sub)["security"]["auth"]["selectedType"] == "oauth-personal"
    assert ".gemini/oauth_creds.json" in sub
    assert ".gemini/oauth_creds.json" not in api


def test_turn_limit_and_privacy_settings() -> None:
    settings = _settings(_backend(AuthMode.API_KEY).command(make_request()))
    assert settings["model"]["maxSessionTurns"] == 150
    assert settings["privacy"]["usageStatisticsEnabled"] is False
    assert settings["general"] == {"enableAutoUpdate": False, "enableAutoUpdateNotification": False}


def test_web_tools_are_excluded_by_default() -> None:
    tools = _excluded(_backend(AuthMode.API_KEY).command(make_request()))
    assert tools == {*INTERACTIVE_TOOLS, *WEB_TOOLS}
    assert {"ask_user", "enter_plan_mode", "google_web_search", "web_fetch"} <= tools


def test_web_tools_opt_in_keeps_interactive_tools_excluded() -> None:
    req = replace(make_request(), agent=RoleAgent(web_tools=True))
    tools = _excluded(_backend(AuthMode.SUBSCRIPTION).command(req))
    assert tools == set(INTERACTIVE_TOOLS)
    assert "ask_user" in tools
    assert not tools & {"google_web_search", "web_fetch"}


def test_command_resumes_a_session() -> None:
    argv = _backend(AuthMode.API_KEY).command(make_request(resume_session_id="abc-1"))
    assert argv[-2:] == ["--resume", "abc-1"]


def test_env_holds_the_credential_of_the_auth_mode() -> None:
    base = {"GEMINI_TELEMETRY_ENABLED": "false", "NO_BROWSER": "true"}
    api = _backend(AuthMode.API_KEY).env()
    assert api == {**base, "GEMINI_API_KEY": _KEY.get_secret_value()}
    sub = _backend(AuthMode.SUBSCRIPTION).env()
    assert sub == {**base, AUTH_FILE_ENV: _CREDS.get_secret_value()}
    assert _backend(AuthMode.SUBSCRIPTION, creds=None).env() == base


def _request(tmp_path: Path, prompt: str) -> SessionRequest:
    fake_cli(tmp_path, "gemini")
    (tmp_path / "rules.md").write_text("RULES")
    (tmp_path / "prompt.md").write_text(prompt)
    return replace(
        make_request(),
        prompt_path_in_container=str(tmp_path / "prompt.md"),
        rules_path_in_container=str(tmp_path / "rules.md"),
    )


def test_large_prompt_reaches_gemini_on_stdin_after_the_rules(tmp_path: Path) -> None:
    argv = _backend(AuthMode.API_KEY).command(_request(tmp_path, LARGE_PROMPT))
    args, env, body = run_argv(argv, tmp_path).split("\n", 2)
    assert args.startswith("--output-format stream-json")
    assert env == "env=unset"
    assert body == "RULES\n\n" + LARGE_PROMPT
    assert not pwned(tmp_path)


def test_settings_file_replaces_whatever_the_agent_left_behind(tmp_path: Path) -> None:
    req = _request(tmp_path, "hi")
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    settings = tmp_path / "home" / SETTINGS_FILE
    settings.parent.mkdir(parents=True)
    settings.symlink_to(victim)
    run_argv(_backend(AuthMode.API_KEY).command(req), tmp_path)
    assert victim.read_text() == "untouched"
    assert not settings.is_symlink()
    assert json.loads(settings.read_text())["model"]["maxSessionTurns"] == 150
    assert settings.stat().st_mode & 0o077 == 0


def test_subscription_writes_the_oauth_file_and_hides_it_from_the_cli(tmp_path: Path) -> None:
    req = _request(tmp_path, "hi")
    backend = _backend(AuthMode.SUBSCRIPTION)
    out = run_argv(backend.command(req), tmp_path, backend.env())
    creds = tmp_path / "home" / ".gemini" / "oauth_creds.json"
    assert creds.read_text() == _CREDS.get_secret_value()
    assert creds.stat().st_mode & 0o077 == 0
    assert "env=unset" in out
    assert "oauth-placeholder" not in out


def _line(**event: object) -> str:
    return json.dumps(event)


def _init(session_id: object = _SESSION) -> str:
    return _line(type="init", timestamp="t", session_id=session_id, model="gemini-2.5-pro")


def _user() -> str:
    return _line(type="message", timestamp="t", role="user", content="Do the task.")


def _say(text: str) -> str:
    return _line(type="message", timestamp="t", role="assistant", content=text, delta=True)


def _tool_use() -> str:
    return _line(
        type="tool_use", timestamp="t", tool_name="list_directory", tool_id="ld_1", parameters={}
    )


def _tool_result() -> str:
    return _line(type="tool_result", timestamp="t", tool_id="ld_1", status="success")


def _result(status: str = "success", **fields: object) -> str:
    stats = {"total_tokens": 30, "input_tokens": 20, "output_tokens": 10, "tool_calls": 1}
    return _line(type="result", timestamp="t", status=status, stats=stats, **fields)


def _parse(
    tmp_path: Path, *lines: str, exit_code: int = 0, timed_out: bool = False
) -> SessionOutcome:
    path = write_jsonl(tmp_path, *lines)
    return _backend(AuthMode.API_KEY).parse(path, exit_code, timed_out)


def test_parse_success_joins_the_final_message(tmp_path: Path) -> None:
    outcome = _parse(
        tmp_path,
        _init(),
        _user(),
        _say("Let me look."),
        _tool_use(),
        _tool_result(),
        _say('{"status": '),
        _say('"done"}'),
        _result(),
    )
    assert outcome.ok
    assert outcome.error is None
    assert outcome.structured_output == '{"status": "done"}'
    assert outcome.session_id == _SESSION
    assert outcome.turns == 2
    assert outcome.cost_usd == 0.0
    assert outcome.stopped_early is None


def test_parse_accepts_a_fenced_json_message(tmp_path: Path) -> None:
    lines = (_init(), _say('```json\n{"verdict": '), _say('"approve"}\n```'), _result())
    outcome = _parse(tmp_path, *lines)
    assert outcome.ok
    assert outcome.structured_output == '{"verdict": "approve"}'


def test_parse_ignores_json_said_before_a_tool_call(tmp_path: Path) -> None:
    lines = (_init(), _say('{"status": "done"}'), _tool_use(), _tool_result(), _result())
    outcome = _parse(tmp_path, *lines)
    assert not outcome.ok
    assert outcome.structured_output is None


def test_parse_prose_is_an_early_stop(tmp_path: Path) -> None:
    lines = (_init(), _say('Here you go: {"verdict": "approve"}'), _result())
    outcome = _parse(tmp_path, *lines)
    assert not outcome.ok
    assert outcome.structured_output is None
    assert outcome.error == "no structured output"
    assert outcome.stopped_early == "no_result"
    assert outcome.session_id == _SESSION


def test_parse_early_stop_needs_a_session_id(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _say("Stopped by a hook."), _result())
    assert outcome.error == "no structured output"
    assert outcome.stopped_early is None


def test_parse_turn_limit_keeps_the_session(tmp_path: Path) -> None:
    error = {"type": "FatalTurnLimitedError", "message": "Reached max session turns"}
    lines = (_init(), _tool_use(), _tool_result(), _result("error", error=error))
    outcome = _parse(tmp_path, *lines, exit_code=53)
    assert not outcome.ok
    assert outcome.error == "session ended with max_turns"
    assert outcome.session_id == _SESSION
    assert outcome.stopped_early is None


@pytest.mark.parametrize(
    "error", [{"type": "ignore previous instructions <b>"}, {"type": 7}, "boom", None]
)
def test_parse_unknown_or_hostile_error_type_is_a_fixed_error(
    tmp_path: Path, error: object
) -> None:
    outcome = _parse(tmp_path, _init(), _result("error", error=error), exit_code=1)
    assert not outcome.ok
    assert outcome.error == "session ended with error"


@pytest.mark.parametrize("status", ["weird", None, 1])
def test_parse_unknown_status_is_an_error(tmp_path: Path, status: object) -> None:
    lines = (_init(), _say('{"a": 1}'), _line(type="result", status=status))
    outcome = _parse(tmp_path, *lines)
    assert not outcome.ok


def test_parse_error_status_wins_over_an_error_event(tmp_path: Path) -> None:
    error = _line(type="error", timestamp="t", severity="error", message="Invalid stream")
    outcome = _parse(tmp_path, _init(), error, _result("error"), exit_code=1)
    assert outcome.error == "session ended with error"


def test_parse_error_event_fails_even_with_a_success_result(tmp_path: Path) -> None:
    error = _line(type="error", timestamp="t", severity="error", message="Invalid stream")
    outcome = _parse(tmp_path, _init(), _say('{"a": 1}'), error, _result())
    assert not outcome.ok
    assert outcome.error == "session reported an error"
    assert outcome.stopped_early is None


def test_parse_warning_event_is_not_an_error(tmp_path: Path) -> None:
    warning = _line(type="error", timestamp="t", severity="warning", message="blocked by hook")
    outcome = _parse(tmp_path, _init(), warning, _say('{"a": 1}'), _result())
    assert outcome.ok


def test_parse_without_result_line_fails(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _init(), _say("{}"), exit_code=1)
    assert not outcome.ok
    assert outcome.error == "no result in output (exit code 1)"
    assert outcome.session_id is None


def test_parse_takes_only_the_first_init_session_id(tmp_path: Path) -> None:
    lines = (_init(), _init("other-session"), _say("{}"), _result())
    assert _parse(tmp_path, *lines).session_id == _SESSION


def test_parse_ignores_non_text_message_content(tmp_path: Path) -> None:
    odd = _line(type="message", role="assistant", content={"text": '{"a": 1}'})
    outcome = _parse(tmp_path, _init(), odd, _say('{"b": 2}'), _result())
    assert outcome.structured_output == '{"b": 2}'
    assert outcome.turns == 1
