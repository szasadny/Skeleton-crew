import json
from pathlib import Path

import pytest
from fake_cli import fake_cli, pwned, run_argv

from skeleton_crew.agents.backends._shared import (
    AUTH_FILE_ENV,
    AuthMode,
    CliFiles,
    auth_mode_option,
    extract_json_text,
    json_object,
    jsonl_events,
    number,
    safe_reason,
    safe_session_id,
    stdin_command,
)

_HOSTILE = "$(touch pwned) `touch pwned2` ; touch pwned3"
_AUTH_FILE = ".cli/auth.json"


@pytest.fixture
def cli(tmp_path: Path) -> None:
    fake_cli(tmp_path, "fake-cli")


def _prompt(tmp_path: Path, text: str = "hi") -> str:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(text)
    return str(prompt)


def _with_auth(tmp_path: Path, extras: str) -> CliFiles:
    rules = tmp_path / "rules.md"
    rules.write_text("RULES")
    return CliFiles(
        _prompt(tmp_path),
        rules_path=str(rules) if "rules" in extras else None,
        auth_file=_AUTH_FILE,
        config_file=(".cli/settings.json", "{}") if "config" in extras else None,
    )


_AUTH_COMBINATIONS = ["", "rules", "config", "rules config"]


@pytest.mark.usefixtures("cli")
def test_prompt_goes_to_stdin_and_args_pass_through(tmp_path: Path) -> None:
    prompt = tmp_path / f"prompt {_HOSTILE}.md"
    prompt.write_text("x" * 300_000 + _HOSTILE)
    argv = stdin_command("fake-cli", ["--flag", _HOSTILE], CliFiles(str(prompt)))
    assert not any("x" * 1000 in arg for arg in argv)
    args, env, body = run_argv(argv, tmp_path).split("\n", 2)
    assert args == f"--flag {_HOSTILE}"
    assert env == "env=unset"
    assert body == "x" * 300_000 + _HOSTILE
    assert not pwned(tmp_path)


@pytest.mark.usefixtures("cli")
def test_rules_are_piped_before_the_prompt(tmp_path: Path) -> None:
    rules = tmp_path / f"rules {_HOSTILE}"
    rules.write_text("RULES")
    files = CliFiles(_prompt(tmp_path, "PROMPT"), rules_path=str(rules))
    assert run_argv(stdin_command("fake-cli", [], files), tmp_path).endswith("RULES\n\nPROMPT")
    assert not pwned(tmp_path)


@pytest.mark.usefixtures("cli")
@pytest.mark.parametrize("extras", _AUTH_COMBINATIONS)
def test_auth_file_is_written_private_and_hidden_from_the_cli_env(
    tmp_path: Path, extras: str
) -> None:
    argv = stdin_command("fake-cli", ["--a"], _with_auth(tmp_path, extras))
    out = run_argv(argv, tmp_path, {AUTH_FILE_ENV: '{"token": "s3cret $(touch pwned)"}'})
    auth = tmp_path / "home" / _AUTH_FILE
    assert auth.read_text() == '{"token": "s3cret $(touch pwned)"}'
    assert auth.stat().st_mode & 0o077 == 0
    assert out.startswith("--a\nenv=unset\n")
    assert "s3cret" not in out
    assert not pwned(tmp_path)


@pytest.mark.usefixtures("cli")
@pytest.mark.parametrize("extras", _AUTH_COMBINATIONS)
def test_auth_file_refreshed_by_the_cli_is_never_overwritten(tmp_path: Path, extras: str) -> None:
    auth = tmp_path / "home" / _AUTH_FILE
    auth.parent.mkdir(parents=True)
    auth.write_text("refreshed")
    run_argv(
        stdin_command("fake-cli", [], _with_auth(tmp_path, extras)),
        tmp_path,
        {AUTH_FILE_ENV: "original"},
    )
    assert auth.read_text() == "refreshed"


@pytest.mark.usefixtures("cli")
def test_config_file_is_rewritten_every_session_and_never_follows_a_symlink(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    config = tmp_path / "home" / ".cli" / "settings.json"
    config.parent.mkdir(parents=True)
    config.symlink_to(victim)
    content = '{"limit": 3, "x": "$(touch pwned) %s"}'
    files = CliFiles(_prompt(tmp_path), config_file=(".cli/settings.json", content))
    assert run_argv(stdin_command("fake-cli", ["--a"], files), tmp_path).startswith("--a\n")
    assert victim.read_text() == "untouched"
    assert not config.is_symlink()
    assert config.read_text() == content
    assert config.stat().st_mode & 0o077 == 0
    assert not pwned(tmp_path)


@pytest.mark.parametrize("binary", ["", "rm -rf /", "a;b", "$(x)", "-p", "Claude"])
def test_hostile_binary_name_is_refused(binary: str) -> None:
    with pytest.raises(ValueError, match="invalid binary"):
        stdin_command(binary, [], CliFiles("/p"))


def test_jsonl_events_skips_garbage(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    lines = ["not json", '{"a": 1}', "[1]", '"s"', "{broken", "\x00\xff", '{"b": 2}']
    path.write_text("\n".join(lines))
    assert list(jsonl_events(path)) == [{"a": 1}, {"b": 2}]
    assert list(jsonl_events(tmp_path / "absent")) == []


def test_jsonl_events_skips_a_line_nested_too_deep(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_text("\n".join(['{"a": 1}', '{"x":' + "[" * 100_000, '{"b": 2}']))
    assert list(jsonl_events(path)) == [{"a": 1}, {"b": 2}]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("abc-1_2", "abc-1_2"), ("--resume", None), ("a b", None), ("x" * 101, None), (7, None)],
)
def test_safe_session_id(value: object, expected: str | None) -> None:
    assert safe_session_id(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("hook_stopped", "hook_stopped"), ("Ignore <b>", None), ("x" * 41, None), (None, None)],
)
def test_safe_reason(value: object, expected: str | None) -> None:
    assert safe_reason(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(2, 2.0), (0.5, 0.5), (True, 0.0), ("1", 0.0)])
def test_number_rejects_non_numbers(value: object, expected: float) -> None:
    assert number(value) == expected


@pytest.mark.parametrize("default", list(AuthMode))
def test_auth_mode_option_lists_the_default_first(default: AuthMode) -> None:
    option = auth_mode_option(default)
    assert option.default == default.value
    assert option.allowed[0] == default.value
    assert sorted(option.allowed) == sorted(mode.value for mode in AuthMode)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  \n{"a": [1, 2]}\n', '{"a": [1, 2]}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('\n```json\n{\n  "a": 1\n}\n```\n', '{"a": 1}'),
    ],
)
def test_extracts_a_whole_or_single_fenced_object(answer: str, expected: str) -> None:
    assert extract_json_text(answer) == expected


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "",
        "Done.",
        'Result: {"a": 1}',
        '{"a": 1} trailing',
        "[1, 2]",
        '"text"',
        '```\n{"a": 1}\n```',
        '```json\n{"a": 1}\n```\nmore text',
        '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```',
        '```json\n{"a": 1\n```',
    ],
)
def test_rejects_anything_else(answer: str | None) -> None:
    assert extract_json_text(answer) is None


def _too_deep(_text: str) -> object:
    raise RecursionError


def test_json_nested_too_deep_for_the_parser_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json, "loads", _too_deep)

    assert json_object('{"a": 1}') is None
    assert extract_json_text('{"a": 1}') is None


@pytest.mark.parametrize("wrap", ["{}", "```json\n{}\n```"])
def test_deeply_nested_json_never_raises(wrap: str) -> None:
    nested = '{"a": ' + "[" * 100_000 + "]" * 100_000 + "}"

    assert extract_json_text(wrap.replace("{}", nested)) in {None, nested}
