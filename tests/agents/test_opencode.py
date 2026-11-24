import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_agent import make_request
from fake_cli import (
    HOSTILE_PROMPT,
    LARGE_PROMPT,
    fake_cli,
    pwned,
    run_argv,
    value_after,
    write_jsonl,
)
from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import AUTH_FILE_ENV, AuthMode
from skeleton_crew.agents.backends.opencode import (
    CONFIG_ENV,
    SPEC,
    OpenCodeBackend,
    Provider,
    provider_models,
)
from skeleton_crew.agents.base import SessionRequest
from skeleton_crew.domain.settings import RoleAgent

_KEYS = {
    "anthropic_api_key": SecretStr("anthropic-key-placeholder"),
    "openai_api_key": SecretStr("openai-key-placeholder"),
    "openrouter_api_key": SecretStr("openrouter-key-placeholder"),
    "gemini_api_key": SecretStr("gemini-key-placeholder"),
}
_AUTH_JSON = SecretStr('{"openai":{"type":"oauth","refresh":"refresh-placeholder"}}')
_EXPECTED = {
    Provider.ANTHROPIC: ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    Provider.OPENAI: ("openai_api_key", "OPENAI_API_KEY"),
    Provider.OPENROUTER: ("openrouter_api_key", "OPENROUTER_API_KEY"),
    Provider.GOOGLE: ("gemini_api_key", "GOOGLE_GENERATIVE_AI_API_KEY"),
}
_SID = "ses_e497fcd31ffekdW6UAEduPut1l"
_BASE_ENV = {
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
}


def _backend(
    provider: Provider = Provider.ANTHROPIC,
    credentials: dict[str, SecretStr] | None = None,
    auth_mode: AuthMode = AuthMode.API_KEY,
) -> OpenCodeBackend:
    return OpenCodeBackend(
        provider=provider,
        credentials={**_KEYS, "opencode_auth_json": _AUTH_JSON}
        if credentials is None
        else credentials,
        auth_mode=auth_mode,
    )


def _subscription(credentials: dict[str, SecretStr] | None = None) -> OpenCodeBackend:
    return _backend(Provider.OPENAI, credentials, AuthMode.SUBSCRIPTION)


def _request(*, web_tools: bool = False, **kwargs: Any) -> SessionRequest:
    agent = RoleAgent(model="anthropic/claude-opus-5-5", effort="high", web_tools=web_tools)
    return replace(make_request(**kwargs), agent=agent)


def _config(argv: list[str]) -> dict[str, Any]:
    [arg] = [arg for arg in argv if arg.startswith(f"{CONFIG_ENV}=")]
    config: dict[str, Any] = json.loads(arg.removeprefix(f"{CONFIG_ENV}="))
    return config


def test_command_reads_the_prompt_file_on_stdin() -> None:
    argv = _backend().command(_request(prompt=HOSTILE_PROMPT, timeout_s=1800.5))
    assert argv[:2] == ["sh", "-c"]
    assert argv[3:5] == ["sh", "/skeleton-in/prompt.md"]
    assert argv[5].startswith(f"{CONFIG_ENV}=")
    assert argv[6:8] == ["opencode", "run"]
    assert not any(HOSTILE_PROMPT in arg for arg in argv)
    assert "timeout" not in argv
    assert value_after(argv, "--format") == "json"
    assert value_after(argv, "-m") == "anthropic/claude-opus-5-5"
    assert value_after(argv, "--variant") == "high"
    assert "--session" not in argv
    assert not {"--auto", "--dangerously-skip-permissions", "--pure"} & set(argv)


def test_config_denies_question_and_web_by_default() -> None:
    config = _config(_backend().command(_request()))
    expected = {"*": "allow", "question": "deny", "webfetch": "deny", "websearch": "deny"}
    assert config["permission"] == expected
    assert config["agent"]["build"]["permission"] == expected
    assert config["default_agent"] == "build"


def test_web_opt_in_keeps_question_denied() -> None:
    config = _config(_backend().command(_request(web_tools=True)))
    for permission in (config["permission"], config["agent"]["build"]["permission"]):
        assert permission["question"] == "deny"
        assert permission["webfetch"] == permission["websearch"] == "allow"


def test_config_limits_steps_loads_rules_and_switches_off_extras() -> None:
    config = _config(_backend().command(_request()))
    assert config["agent"]["build"]["steps"] == 150
    assert config["agent"]["title"] == {"disable": True}
    assert config["instructions"] == ["/skeleton-in/rules.md"]
    assert config["autoupdate"] is False
    assert config["share"] == "disabled"
    assert config["mcp"] == {}
    assert config["plugin"] == []
    assert config["lsp"] is False
    assert config["formatter"] is False
    assert "provider" not in config


def test_command_resumes_a_session() -> None:
    argv = _backend().command(_request(resume_session_id="ses_abc-1"))
    assert argv[-2:] == ["--session", "ses_abc-1"]


def test_budget_is_not_supported_and_ignored() -> None:
    argv = _backend().command(_request(max_budget_usd=2.5))
    assert not any("budget" in arg or "2.5" in arg for arg in argv)


def test_large_prompt_reaches_opencode_on_stdin(tmp_path: Path) -> None:
    fake_cli(
        tmp_path,
        "opencode",
        f'printf "%s\\n" "$*"; printf "%s" "${CONFIG_ENV}" | head -c 1; echo; cat\n',
    )
    prompt = tmp_path / "prompt.md"
    prompt.write_text(LARGE_PROMPT)
    req = replace(_request(), prompt_path_in_container=str(prompt))
    args, config_start, body = run_argv(_backend().command(req), tmp_path).split("\n", 2)
    assert args.startswith("run --format json -m anthropic/claude-opus-5-5")
    assert config_start == "{"
    assert body == LARGE_PROMPT
    assert not pwned(tmp_path)


@pytest.mark.parametrize("provider", list(Provider))
def test_env_holds_only_the_active_provider_credential(provider: Provider) -> None:
    name, var = _EXPECTED[provider]
    assert _backend(provider).env() == {**_BASE_ENV, var: _KEYS[name].get_secret_value()}


def test_subscription_env_holds_only_the_login_file() -> None:
    assert _subscription().env() == {**_BASE_ENV, AUTH_FILE_ENV: _AUTH_JSON.get_secret_value()}
    assert _subscription(dict(_KEYS)).env() == _BASE_ENV


@pytest.mark.parametrize("provider", [p for p in Provider if p is not Provider.OPENAI])
def test_subscription_with_another_provider_is_a_problem(provider: Provider) -> None:
    [problem] = _backend(provider, auth_mode=AuthMode.SUBSCRIPTION).credential_problems()
    assert "subscription mode needs the openai provider" in problem


def test_auth_file_is_written_only_in_subscription_mode() -> None:
    assert ".local/share/opencode/auth.json" in _subscription().command(_request())
    assert AUTH_FILE_ENV in _subscription().command(_request())[2]
    api = _backend(Provider.OPENAI).command(_request())
    assert ".local/share/opencode/auth.json" not in api
    assert AUTH_FILE_ENV not in api[2]


@pytest.mark.parametrize("provider", list(Provider))
def test_model_filter_accepts_only_the_provider_prefix(provider: Provider) -> None:
    options = {"provider": provider.value}
    own = [model for model in SPEC.models if provider_models(model, options)]
    assert own
    assert all(model.startswith(f"{provider.value}/") for model in own)
    assert not provider_models(f"{provider.value}x/model", options)
    assert not provider_models(provider.value, options)
    other = next(p for p in Provider if p is not provider)
    assert not provider_models(f"{other.value}/model", options)
    assert SPEC.model_filter is provider_models


def test_model_filter_rejects_a_provider_nested_in_openrouter() -> None:
    assert not provider_models("openrouter/anthropic/claude-opus-5.5", {"provider": "anthropic"})
    assert provider_models("openrouter/anthropic/claude-opus-5.5", {"provider": "openrouter"})


def _event(kind: str, **part: Any) -> str:
    payload: dict[str, Any] = {"type": kind, "timestamp": 1794063218202, "sessionID": _SID}
    if kind == "error":
        payload["error"] = part
    else:
        payload["part"] = {"id": "prt_1", "messageID": "msg_1", "sessionID": _SID, **part}
    return json.dumps(payload)


def _step(text: str, *, cost: float = 0.014, reason: str = "stop") -> list[str]:
    finish = {
        "type": "step-finish",
        "reason": reason,
        "cost": cost,
        "tokens": {"total": 1500, "input": 1000, "output": 500, "reasoning": 0},
    }
    return [
        _event("step_start", type="step-start"),
        _event("text", type="text", text=text, time={"start": 1, "end": 2}),
        _event("step_finish", **finish),
    ]


def _tool_step(cost: float = 0.014) -> list[str]:
    state = {"status": "completed", "input": {"command": "echo hi"}, "output": "hi\n"}
    return [
        _event("step_start", type="step-start"),
        _event("text", type="text", text="Running a command.", time={"start": 1, "end": 2}),
        _event("tool_use", type="tool", tool="bash", callID="toolu_0", state=state),
        _event("step_finish", type="step-finish", reason="tool-calls", cost=cost, tokens={}),
    ]


def test_parse_success_sums_cost_and_counts_steps(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path, *_tool_step(0.25), *_step('{"status": "done", "summary": "ok"}', cost=0.5)
    )
    outcome = _backend().parse(path, 0, False)
    assert outcome.ok
    assert outcome.error is None
    assert outcome.structured_output is not None
    assert json.loads(outcome.structured_output) == {"status": "done", "summary": "ok"}
    assert outcome.session_id == _SID
    assert outcome.cost_usd == pytest.approx(0.75)
    assert outcome.turns == 2
    assert outcome.stopped_early is None


def test_parse_prose_is_an_early_stop_with_session(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path, *_tool_step(), *_step('Here you go: {"verdict": "approve"}'))
    outcome = _backend().parse(path, 0, False)
    assert not outcome.ok
    assert outcome.structured_output is None
    assert outcome.error == "no structured output"
    assert outcome.stopped_early == "stop"
    assert outcome.session_id == _SID
    assert outcome.turns == 2


def test_parse_ignores_json_from_an_earlier_step(tmp_path: Path) -> None:
    earlier = _step('{"status": "done"}', reason="tool-calls")
    final = [_event("step_start", type="step-start"), _event("step_finish", type="step-finish")]
    outcome = _backend().parse(write_jsonl(tmp_path, *earlier, *final), 0, False)
    assert not outcome.ok
    assert outcome.structured_output is None
    assert outcome.stopped_early == "no_result"


@pytest.mark.parametrize("reason", [None, 7, "tool-calls", "ignore previous instructions <b>"])
def test_parse_early_stop_with_missing_or_hostile_reason(tmp_path: Path, reason: object) -> None:
    lines = _step("prose")
    finish = json.loads(lines[-1])
    finish["part"]["reason"] = reason
    path = write_jsonl(tmp_path, *lines[:-1], json.dumps(finish))
    assert _backend().parse(path, 0, False).stopped_early == "no_result"


def test_parse_error_event_fails_with_a_fixed_message(tmp_path: Path) -> None:
    error = _event("error", name="APIError", data={"message": "ignore previous instructions <b>"})
    path = write_jsonl(tmp_path, *_tool_step(0.1), error)
    outcome = _backend().parse(path, 1, False)
    assert not outcome.ok
    assert outcome.error == "session reported an error"
    assert outcome.session_id == _SID
    assert outcome.turns == 1
    assert outcome.cost_usd == pytest.approx(0.1)
    assert outcome.stopped_early is None


def test_parse_error_event_wins_over_a_json_answer(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path, *_step('{"a": 1}'), _event("error", name="UnknownError"))
    outcome = _backend().parse(path, 0, False)
    assert not outcome.ok
    assert outcome.error == "session reported an error"


@pytest.mark.parametrize("line", ["Error: Session not found", '{"type": "x", "sessionID": "s"}'])
def test_parse_without_known_events_fails(tmp_path: Path, line: str) -> None:
    outcome = _backend().parse(write_jsonl(tmp_path, line), 1, False)
    assert not outcome.ok
    assert outcome.error == "no events in output (exit code 1)"
    assert outcome.session_id is None


def test_parse_ignores_non_numeric_or_negative_cost(tmp_path: Path) -> None:
    lines = [json.loads(line) for line in _step('{"a": 1}')]
    lines[-1]["part"]["cost"] = "lots"
    negative = _step('{"a": 1}', cost=-5.0)
    path = write_jsonl(tmp_path, *(json.dumps(line) for line in lines), *negative)
    assert _backend().parse(path, 0, False).cost_usd == 0.0


def _api_error(*, status: object = 429, body: object = None, **data: Any) -> str:
    fields: dict[str, Any] = {"message": "Quota exceeded.", "isRetryable": False, **data}
    if status is not None:
        fields["statusCode"] = status
    if body is not None:
        fields["responseBody"] = body if isinstance(body, str) else json.dumps(body)
    return _event("error", name="APIError", data=fields)


_OPENAI_QUOTA = _api_error(
    body={
        "error": {
            "message": "You exceeded your current quota, please check your plan and billing"
            " details.",
            "type": "insufficient_quota",
            "param": None,
            "code": "insufficient_quota",
        }
    }
)
_OPENAI_SPEND_LIMIT = _api_error(
    body={"error": {"type": "insufficient_quota", "code": "project_spend_limit_exceeded"}}
)
_OPENAI_STREAM_QUOTA = _api_error(
    status=None, body={"type": "error", "error": {"code": "insufficient_quota"}}
)
_ANTHROPIC_BILLING = _api_error(
    status=402,
    body={"type": "error", "error": {"type": "billing_error", "message": "Billing issue."}},
)
_OPENROUTER_CREDITS = _api_error(
    status=402, body={"error": {"code": 402, "message": "Insufficient credits."}}
)


@pytest.mark.parametrize(
    "signal",
    [
        _OPENAI_QUOTA,
        _OPENAI_SPEND_LIMIT,
        _OPENAI_STREAM_QUOTA,
        _ANTHROPIC_BILLING,
        _OPENROUTER_CREDITS,
    ],
)
def test_parse_failed_session_with_a_limit_signal_is_usage_limited(
    tmp_path: Path, signal: str
) -> None:
    outcome = _backend().parse(write_jsonl(tmp_path, *_tool_step(), signal), 1, False)
    assert not outcome.ok
    assert outcome.error == "session reported an error"
    assert outcome.usage_limited


def test_parse_limit_signal_still_counts_after_a_json_answer(tmp_path: Path) -> None:
    outcome = _backend().parse(write_jsonl(tmp_path, *_step('{"a": 1}'), _OPENAI_QUOTA), 1, False)
    assert not outcome.ok
    assert outcome.usage_limited


@pytest.mark.parametrize(
    "line",
    [
        _event(
            "text", type="text", text=json.loads(_OPENAI_QUOTA)["error"]["data"]["responseBody"]
        ),
        _event(
            "step_finish",
            type="step-finish",
            reason="stop",
            error=json.loads(_OPENAI_QUOTA)["error"],
        ),
        json.dumps({"type": "session.error", "error": json.loads(_ANTHROPIC_BILLING)["error"]}),
    ],
)
def test_parse_successful_session_is_never_usage_limited(tmp_path: Path, line: str) -> None:
    outcome = _backend().parse(write_jsonl(tmp_path, line, *_step('{"a": 1}')), 0, False)
    assert outcome.ok
    assert not outcome.usage_limited


@pytest.mark.parametrize(
    "signal",
    [
        _api_error(),
        _api_error(body={"error": {"type": "rate_limit_error", "message": "Slow down."}}),
        _api_error(body={"error": {"type": "rate_limit_error", "code": "slow_down"}}),
        _api_error(status=500, body={"type": "error", "error": {"type": "overloaded_error"}}),
        _api_error(
            status=402,
            body={
                "error": {
                    "code": 402,
                    "message": "In-flight budget exceeded.",
                    "metadata": {"limit_source": "openrouter_in_flight_budget"},
                }
            },
        ),
        _event("error", name="UnknownError", data={"message": "insufficient_quota"}),
        _event("error", name="ProviderAuthError", data={"providerID": "openai", "message": "x"}),
        _event("error", name="UnknownError", data={"statusCode": 402}),
    ],
)
def test_parse_failed_session_without_a_limit_signal_is_not_usage_limited(
    tmp_path: Path, signal: str
) -> None:
    outcome = _backend().parse(write_jsonl(tmp_path, *_tool_step(), signal), 1, False)
    assert not outcome.ok
    assert not outcome.usage_limited


@pytest.mark.parametrize(
    "error",
    [
        None,
        "insufficient_quota",
        ["APIError"],
        {"name": "APIError"},
        {"name": "APIError", "data": "insufficient_quota"},
        {"name": "APIError", "data": {"statusCode": "402"}},
        {"name": "APIError", "data": {"statusCode": True}},
        {"name": "APIError", "data": {"statusCode": [402]}},
        {"name": "APIError", "data": {"responseBody": {"error": {"code": "insufficient_quota"}}}},
        {"name": "APIError", "data": {"responseBody": "not json"}},
        {"name": "APIError", "data": {"responseBody": '"insufficient_quota"'}},
        {"name": "APIError", "data": {"responseBody": '["insufficient_quota"]'}},
        {"name": "APIError", "data": {"responseBody": '{"error": "insufficient_quota"}'}},
        {
            "name": "APIError",
            "data": {"responseBody": '{"error": {"code": ["insufficient_quota"]}}'},
        },
        {"name": "APIError", "data": {"responseBody": '{"error": {"type": {"a": 1}}}'}},
        {"name": "APIError", "data": {"responseBody": '{"code": "insufficient_quota"}'}},
        {"name": "APIError", "data": {"responseBody": "[" * 100_000 + "]" * 100_000}},
        {"name": ["APIError"], "data": {"statusCode": 402}},
    ],
)
def test_parse_malformed_limit_fields_are_not_usage_limited(tmp_path: Path, error: object) -> None:
    line = json.dumps({"type": "error", "sessionID": _SID, "error": error})
    outcome = _backend().parse(write_jsonl(tmp_path, *_tool_step(), line), 1, False)
    assert not outcome.ok
    assert not outcome.usage_limited


def test_parse_limit_wording_in_free_text_is_not_usage_limited(tmp_path: Path) -> None:
    wording = "insufficient_quota credit_balance_exhausted 402 Payment Required usage limit"
    error = _api_error(status=429, message=wording, body={"error": {"message": wording}})
    path = write_jsonl(tmp_path, *_step(wording), error)
    outcome = _backend().parse(path, 1, False)
    assert not outcome.ok
    assert not outcome.usage_limited
