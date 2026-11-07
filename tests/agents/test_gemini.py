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
from skeleton_crew.agents.backends.gemini import (
    INTERACTIVE_TOOLS,
    SETTINGS_FILE,
    SPEC,
    WEB_TOOLS,
    AuthMode,
    GeminiBackend,
)
from skeleton_crew.agents.base import SessionOutcome
from skeleton_crew.domain.settings import RoleAgent

_KEY = SecretStr("api-key-placeholder")
_CREDS = SecretStr('{"refresh_token": "oauth-placeholder"}')
_SESSION = "8cc0c750-3dd2-45ba-9b04-418d6f2495bc"


def _backend(
    mode: AuthMode, *, key: SecretStr | None = _KEY, creds: SecretStr | None = _CREDS
) -> GeminiBackend:
    credentials = {"gemini_api_key": key, "gemini_oauth_creds_json": creds}
    return GeminiBackend(
        auth_mode=mode, credentials={k: v for k, v in credentials.items() if v is not None}
    )


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _settings(argv: list[str]) -> dict[str, Any]:
    settings: dict[str, Any] = json.loads(_value_after(argv, SETTINGS_FILE))
    return settings


def _excluded(argv: list[str]) -> set[str]:
    return set(_settings(argv)["tools"]["exclude"])


@pytest.mark.parametrize("mode", list(AuthMode))
def test_command_pipes_rules_and_prompt_on_stdin(mode: AuthMode) -> None:
    prompt = "Fix it; $(rm -rf /) 'q' \"qq\"\nsecond line"
    argv = _backend(mode).command(make_request(prompt=prompt, timeout_s=1800.5))
    assert argv[:2] == ["sh", "-c"]
    assert argv[3:6] == ["sh", "/skeleton-in/prompt.md", "/skeleton-in/rules.md"]
    assert not any(prompt in arg for arg in argv)
    assert "timeout" not in argv
    assert "-p" not in argv
    assert _value_after(argv, "--output-format") == "stream-json"
    assert _value_after(argv, "--model") == "opus"
    assert _value_after(argv, "--approval-mode") == "yolo"
    assert "--skip-trust" in argv
    assert _value_after(argv, "--allowed-mcp-server-names") == "skeleton-crew-no-mcp"
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


@pytest.mark.parametrize("mode", list(AuthMode))
def test_no_credential_ever_in_argv(mode: AuthMode) -> None:
    argv = _backend(mode).command(make_request(max_budget_usd=1.0, resume_session_id="s-1"))
    for secret in (_KEY, _CREDS):
        assert not any(secret.get_secret_value() in arg for arg in argv)


def test_env_holds_only_the_credential_of_the_auth_mode() -> None:
    api = _backend(AuthMode.API_KEY).env()
    assert api["GEMINI_API_KEY"] == _KEY.get_secret_value()
    assert AUTH_FILE_ENV not in api
    sub = _backend(AuthMode.SUBSCRIPTION).env()
    assert sub[AUTH_FILE_ENV] == _CREDS.get_secret_value()
    assert "GEMINI_API_KEY" not in sub
    for env in (api, sub):
        assert env["GEMINI_TELEMETRY_ENABLED"] == "false"
        assert env["NO_BROWSER"] == "true"
        assert len(env) == 3


def test_missing_credential_is_not_put_in_env() -> None:
    assert "GEMINI_API_KEY" not in _backend(AuthMode.API_KEY, key=None).env()
    assert AUTH_FILE_ENV not in _backend(AuthMode.SUBSCRIPTION, creds=None).env()


def test_credential_problems_per_auth_mode() -> None:
    assert _backend(AuthMode.API_KEY, creds=None).credential_problems() == []
    assert _backend(AuthMode.SUBSCRIPTION, key=None).credential_problems() == []
    [api_problem] = _backend(AuthMode.API_KEY, key=None).credential_problems()
    assert "SC_AGENT_CREDENTIALS__GEMINI_API_KEY" in api_problem
    [sub_problem] = _backend(AuthMode.SUBSCRIPTION, creds=SecretStr(" \n")).credential_problems()
    assert "SC_AGENT_CREDENTIALS__GEMINI_OAUTH_CREDS_JSON" in sub_problem


def test_egress_per_auth_mode() -> None:
    assert _backend(AuthMode.API_KEY).egress_domains() == ("generativelanguage.googleapis.com",)
    assert _backend(AuthMode.SUBSCRIPTION).egress_domains() == (
        "cloudcode-pa.googleapis.com",
        "oauth2.googleapis.com",
    )


def test_auto_loaded_docs_are_gemini_md() -> None:
    assert _backend(AuthMode.API_KEY).auto_loaded_docs() == ("GEMINI.md",)


def test_spec_is_consistent() -> None:
    defaults = {key: option.default for key, option in SPEC.options.items()}
    assert defaults == {"auth_mode": "api_key"}
    backend = SPEC.factory({}, defaults)
    assert backend.name == SPEC.name == "gemini"
    assert backend.egress_domains() == ("generativelanguage.googleapis.com",)
    assert set(SPEC.credentials) == {"gemini_api_key", "gemini_oauth_creds_json"}
    assert SPEC.efforts == ("default",)
    assert {"auto", "pro", "flash", "gemini-2.5-pro"} <= set(SPEC.models)
    assert SPEC.web_tools


@pytest.fixture
def fake_gemini(tmp_path: Path) -> Path:
    if shutil.which("sh") is None:
        pytest.skip("sh not installed")
    gemini = tmp_path / "bin" / "gemini"
    gemini.parent.mkdir()
    gemini.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*"; printf "env=%s\\n" "${{{AUTH_FILE_ENV}:-unset}}"; cat\n'
    )
    gemini.chmod(0o755)
    (tmp_path / "home").mkdir()
    return gemini


def _run(argv: list[str], tmp_path: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(  # noqa: S603
        argv,
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            **(env or {}),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _files(tmp_path: Path, prompt: str) -> Path:
    (tmp_path / "rules.md").write_text("RULES")
    path = tmp_path / "prompt.md"
    path.write_text(prompt)
    return path


@pytest.mark.usefixtures("fake_gemini")
def test_large_prompt_reaches_gemini_on_stdin_after_the_rules(tmp_path: Path) -> None:
    prompt = _files(tmp_path, "x" * 300_000 + " $(touch pwned)")
    req = replace(
        make_request(),
        prompt_path_in_container=str(prompt),
        rules_path_in_container=str(tmp_path / "rules.md"),
    )
    argv = _backend(AuthMode.API_KEY).command(req)
    args, env, body = _run(argv, tmp_path).split("\n", 2)
    assert args.startswith("--output-format stream-json")
    assert env == "env=unset"
    assert body == "RULES\n\n" + "x" * 300_000 + " $(touch pwned)"
    assert not (tmp_path / "pwned").exists()


@pytest.mark.usefixtures("fake_gemini")
def test_settings_file_replaces_whatever_the_agent_left_behind(tmp_path: Path) -> None:
    prompt = _files(tmp_path, "hi")
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    settings = tmp_path / "home" / SETTINGS_FILE
    settings.parent.mkdir()
    settings.symlink_to(victim)
    req = replace(make_request(), prompt_path_in_container=str(prompt))
    req = replace(req, rules_path_in_container=str(tmp_path / "rules.md"))
    _run(_backend(AuthMode.API_KEY).command(req), tmp_path)
    assert victim.read_text() == "untouched"
    assert not settings.is_symlink()
    assert json.loads(settings.read_text())["model"]["maxSessionTurns"] == 150
    assert settings.stat().st_mode & 0o077 == 0


@pytest.mark.usefixtures("fake_gemini")
def test_subscription_writes_the_oauth_file_and_hides_it_from_the_cli(tmp_path: Path) -> None:
    prompt = _files(tmp_path, "hi")
    req = replace(
        make_request(),
        prompt_path_in_container=str(prompt),
        rules_path_in_container=str(tmp_path / "rules.md"),
    )
    backend = _backend(AuthMode.SUBSCRIPTION)
    out = _run(backend.command(req), tmp_path, backend.env())
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


def _stream(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "stdout.jsonl"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _parse(
    tmp_path: Path, *lines: str, exit_code: int = 0, timed_out: bool = False
) -> SessionOutcome:
    path = _stream(tmp_path, *lines)
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


@pytest.mark.parametrize(("exit_code", "timed_out"), [(1, False), (0, True), (130, True)])
def test_parse_success_result_still_fails_on_other_error_signals(
    tmp_path: Path, exit_code: int, timed_out: bool
) -> None:
    lines = (_init(), _say('{"a": 1}'), _result())
    outcome = _parse(tmp_path, *lines, exit_code=exit_code, timed_out=timed_out)
    assert not outcome.ok
    assert outcome.timed_out is timed_out
    assert outcome.stopped_early is None


def test_parse_skips_garbage_lines(tmp_path: Path) -> None:
    lines = ("not json", _init(), "{broken", "[1,2]", '"text"', _say('{"n": 2}'), _result())
    outcome = _parse(tmp_path, *lines, "\x00\xff")
    assert outcome.ok
    assert outcome.structured_output == '{"n": 2}'


@pytest.mark.parametrize("content", ["", "garbage\n", f"{_init()}\n{_say('{}')}\n"])
def test_parse_without_result_line_fails(tmp_path: Path, content: str) -> None:
    path = tmp_path / "stdout.jsonl"
    path.write_text(content, encoding="utf-8")
    outcome = _backend(AuthMode.API_KEY).parse(path, 0, False)
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.session_id is None


def test_parse_missing_file_fails(tmp_path: Path) -> None:
    outcome = _backend(AuthMode.API_KEY).parse(tmp_path / "absent", 143, False)
    assert not outcome.ok
    assert outcome.error == "no result in output (exit code 143)"


def test_parse_timeout_without_result(tmp_path: Path) -> None:
    outcome = _parse(tmp_path, _init(), _tool_use(), timed_out=True)
    assert not outcome.ok
    assert outcome.timed_out
    assert outcome.error == "timed out"


@pytest.mark.parametrize("session_id", ["--yolo", "a b", "x" * 101, 7, None])
def test_parse_drops_hostile_session_ids(tmp_path: Path, session_id: object) -> None:
    outcome = _parse(tmp_path, _init(session_id), _say('{"a": 1}'), _result())
    assert outcome.session_id is None


def test_parse_takes_only_the_first_init_session_id(tmp_path: Path) -> None:
    lines = (_init(), _init("other-session"), _say("{}"), _result())
    assert _parse(tmp_path, *lines).session_id == _SESSION


def test_parse_ignores_non_text_message_content(tmp_path: Path) -> None:
    odd = _line(type="message", role="assistant", content={"text": '{"a": 1}'})
    outcome = _parse(tmp_path, _init(), odd, _say('{"b": 2}'), _result())
    assert outcome.structured_output == '{"b": 2}'
    assert outcome.turns == 1
