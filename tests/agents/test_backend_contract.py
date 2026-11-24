import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fake_agent import claude_result_line, make_request
from fake_cli import write_jsonl
from pydantic import SecretStr

from skeleton_crew.agents.base import AgentBackend, BackendSpec
from skeleton_crew.agents.registry import available_backends, backend_for
from skeleton_crew.core.config import credential_setting
from skeleton_crew.domain.settings import RoleAgent

_SESSION = "ses_contract-1"
_SPECS = available_backends()
_SECRETS = {
    name: SecretStr(f"{name}-secret-placeholder") for spec in _SPECS for name in spec.credentials
}
_SECRET_VALUES = {secret.get_secret_value() for secret in _SECRETS.values()}
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_GARBAGE = ("not json", "{broken", "[1,2]", '"text"', '{"x":' + "[" * 100_000, "\x00\xff")


def _claude(session_id: object, answer: str) -> list[str]:
    return [claude_result_line(session_id=session_id, result=answer)]


def _codex(session_id: object, answer: str) -> list[str]:
    item = {"id": "item_1", "type": "agent_message", "text": answer}
    return [
        json.dumps({"type": "thread.started", "thread_id": session_id}),
        json.dumps({"type": "item.completed", "item": item}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]


def _gemini(session_id: object, answer: str) -> list[str]:
    return [
        json.dumps({"type": "init", "session_id": session_id}),
        json.dumps({"type": "message", "role": "assistant", "content": answer}),
        json.dumps({"type": "result", "status": "success"}),
    ]


def _opencode(session_id: object, answer: str) -> list[str]:
    return [
        json.dumps({"type": "step_start", "sessionID": session_id, "part": {}}),
        json.dumps({"type": "text", "sessionID": session_id, "part": {"text": answer}}),
        json.dumps({"type": "step_finish", "sessionID": session_id, "part": {"reason": "stop"}}),
    ]


@dataclass(frozen=True)
class _Contract:
    transcript: Callable[[object, str], list[str]]
    missing: str
    stops_without_session: bool = False


_CONTRACTS = {
    "claude_code": _Contract(_claude, "result", stops_without_session=True),
    "codex": _Contract(_codex, "session"),
    "gemini": _Contract(_gemini, "result"),
    "opencode": _Contract(_opencode, "events"),
}


def _option_sets(spec: BackendSpec) -> list[dict[str, str]]:
    keys = sorted(spec.options)
    choices = itertools.product(*(spec.options[key].allowed for key in keys))
    return [dict(zip(keys, values, strict=True)) for values in choices]


def _backend(
    spec: BackendSpec, options: dict[str, str], secrets: dict[str, SecretStr] = _SECRETS
) -> AgentBackend:
    return backend_for(spec.name, credentials=secrets, options=options)


def _usable_option_sets(spec: BackendSpec) -> list[dict[str, str]]:
    return [o for o in _option_sets(spec) if not _backend(spec, o).credential_problems()]


def _without(name: str) -> dict[str, SecretStr]:
    return {key: value for key, value in _SECRETS.items() if key != name}


def _needed(spec: BackendSpec, options: dict[str, str]) -> list[str]:
    return [
        name
        for name in spec.credentials
        if _backend(spec, options, _without(name)).credential_problems()
    ]


per_backend = pytest.mark.parametrize("spec", _SPECS, ids=lambda spec: spec.name)


def test_every_backend_has_a_contract() -> None:
    assert set(_CONTRACTS) == {spec.name for spec in _SPECS}


@per_backend
def test_spec_is_consistent(spec: BackendSpec) -> None:
    backend = _backend(spec, {})
    assert backend.name == spec.name
    assert backend.credential_problems() == []
    needed = [_needed(spec, options) for options in _usable_option_sets(spec)]
    assert all(len(names) == 1 for names in needed)
    assert {name for names in needed for name in names} == set(spec.credentials)
    for options in _option_sets(spec):
        assert all(_HOST_RE.match(host) for host in _backend(spec, options).egress_domains())
        assert _backend(spec, options).egress_domains()
    for doc in backend.auto_loaded_docs():
        assert doc.endswith(".md")
        assert not doc.startswith("/")


@per_backend
def test_no_credential_ever_in_argv(spec: BackendSpec) -> None:
    for options, web_tools in itertools.product(_option_sets(spec), (False, True)):
        req = replace(
            make_request(max_budget_usd=1.0, resume_session_id="s-1"),
            agent=RoleAgent(web_tools=web_tools),
        )
        argv = _backend(spec, options).command(req)
        assert not any(secret in arg for arg in argv for secret in _SECRET_VALUES)


@per_backend
def test_env_holds_only_the_credential_the_options_need(spec: BackendSpec) -> None:
    for options in _usable_option_sets(spec):
        [name] = _needed(spec, options)
        env = _backend(spec, options).env()
        assert [v for v in env.values() if v in _SECRET_VALUES] == [
            _SECRETS[name].get_secret_value()
        ]


@per_backend
def test_missing_credential_is_named_and_kept_out_of_env(spec: BackendSpec) -> None:
    for options in _usable_option_sets(spec):
        [name] = _needed(spec, options)
        for secrets in (_without(name), {**_without(name), name: SecretStr(" \n")}):
            backend = _backend(spec, options, secrets)
            [problem] = backend.credential_problems()
            assert credential_setting(name) in problem
            assert not set(backend.env().values()) & _SECRET_VALUES


@per_backend
@pytest.mark.parametrize("timed_out", [False, True])
def test_missing_output_file_fails(tmp_path: Path, spec: BackendSpec, timed_out: bool) -> None:
    outcome = _backend(spec, {}).parse(tmp_path / "absent", 143, timed_out)
    assert not outcome.ok
    assert outcome.timed_out is timed_out
    assert outcome.session_id is None
    assert outcome.structured_output is None
    assert outcome.stopped_early is None
    assert not outcome.usage_limited
    missing = f"no {_CONTRACTS[spec.name].missing} in output (exit code 143)"
    assert outcome.error == ("timed out" if timed_out else missing)


@per_backend
@pytest.mark.parametrize("content", ["", "garbage\n", "\n".join(_GARBAGE)])
def test_output_without_events_fails(tmp_path: Path, spec: BackendSpec, content: str) -> None:
    path = tmp_path / "stdout.jsonl"
    path.write_text(content, encoding="utf-8")
    outcome = _backend(spec, {}).parse(path, 0, False)
    assert not outcome.ok
    assert outcome.error == f"no {_CONTRACTS[spec.name].missing} in output (exit code 0)"


@per_backend
def test_garbage_lines_are_skipped(tmp_path: Path, spec: BackendSpec) -> None:
    first, *rest = _CONTRACTS[spec.name].transcript(_SESSION, '{"n": 2}')
    path = write_jsonl(tmp_path, _GARBAGE[0], first, *_GARBAGE[1:4], *rest, *_GARBAGE[4:])
    outcome = _backend(spec, {}).parse(path, 0, False)
    assert outcome.ok
    assert outcome.error is None
    assert outcome.structured_output == '{"n": 2}'
    assert outcome.session_id == _SESSION


@per_backend
@pytest.mark.parametrize(
    "session_id",
    ["--dangerously-skip-permissions", "-", "-s", "a b", "x" * 101, 7, None],
)
def test_hostile_session_id_is_dropped(
    tmp_path: Path, spec: BackendSpec, session_id: object
) -> None:
    contract = _CONTRACTS[spec.name]
    path = write_jsonl(tmp_path, *contract.transcript(session_id, "Done."))
    outcome = _backend(spec, {}).parse(path, 0, False)
    assert outcome.session_id is None
    assert not outcome.ok
    assert outcome.stopped_early == ("no_result" if contract.stops_without_session else None)


@per_backend
@pytest.mark.parametrize(
    ("exit_code", "timed_out", "error"),
    [(0, False, None), (1, False, "exit code 1"), (0, True, "timed out"), (2, True, "timed out")],
)
def test_a_json_answer_still_fails_on_exit_code_and_timeout(
    tmp_path: Path, spec: BackendSpec, exit_code: int, timed_out: bool, error: str | None
) -> None:
    path = write_jsonl(tmp_path, *_CONTRACTS[spec.name].transcript(_SESSION, '{"a": 1}'))
    outcome = _backend(spec, {}).parse(path, exit_code, timed_out)
    assert outcome.ok is (error is None)
    assert outcome.error == error
    assert outcome.timed_out is timed_out
    assert outcome.structured_output == '{"a": 1}'
    assert outcome.session_id == _SESSION
    assert outcome.stopped_early is None
    assert not outcome.usage_limited


@per_backend
def test_prose_is_an_early_stop(tmp_path: Path, spec: BackendSpec) -> None:
    path = write_jsonl(tmp_path, *_CONTRACTS[spec.name].transcript(_SESSION, "Done."))
    outcome = _backend(spec, {}).parse(path, 0, False)
    assert not outcome.ok
    assert outcome.error == "no structured output"
    assert outcome.structured_output is None
    assert outcome.session_id == _SESSION
    assert outcome.stopped_early is not None


@per_backend
@pytest.mark.parametrize(
    "answer", ['```json\n{"n": 1}\n```', '\n```json\n{\n  "n": 1\n}\n```\n', '  {"n": 1}\n']
)
def test_a_single_fenced_or_bare_json_answer_is_accepted(
    tmp_path: Path, spec: BackendSpec, answer: str
) -> None:
    path = write_jsonl(tmp_path, *_CONTRACTS[spec.name].transcript(_SESSION, answer))
    assert _backend(spec, {}).parse(path, 0, False).structured_output == '{"n": 1}'
