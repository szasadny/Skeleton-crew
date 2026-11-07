import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import (
    AUTH_FILE_ENV,
    jsonl_events,
    number,
    safe_reason,
    safe_session_id,
    secret_value,
    stdin_command,
)

_HOSTILE = "$(touch pwned) `touch pwned2` ; touch pwned3"


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    if shutil.which("sh") is None:
        pytest.skip("sh not installed")
    cli = tmp_path / "bin" / "fake-cli"
    cli.parent.mkdir()
    cli.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*"; printf "env=%s\\n" "${{{AUTH_FILE_ENV}:-unset}}"; cat\n'
    )
    cli.chmod(0o755)
    return cli


def _run(argv: list[str], tmp_path: Path, env: dict[str, str] | None = None) -> str:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603
        argv,
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(home), **(env or {})},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _pwned(tmp_path: Path) -> bool:
    return any((tmp_path / name).exists() for name in ("pwned", "pwned2", "pwned3"))


@pytest.mark.usefixtures("fake_cli")
def test_prompt_goes_to_stdin_and_args_pass_through(tmp_path: Path) -> None:
    prompt = tmp_path / f"prompt {_HOSTILE}.md"
    prompt.write_text("x" * 300_000 + _HOSTILE)
    argv = stdin_command("fake-cli", ["--flag", _HOSTILE], prompt_path=str(prompt))
    assert not any("x" * 1000 in arg for arg in argv)
    args, env, body = _run(argv, tmp_path).split("\n", 2)
    assert args == f"--flag {_HOSTILE}"
    assert env == "env=unset"
    assert body == "x" * 300_000 + _HOSTILE
    assert not _pwned(tmp_path)


@pytest.mark.usefixtures("fake_cli")
def test_rules_are_piped_before_the_prompt(tmp_path: Path) -> None:
    rules, prompt = tmp_path / f"rules {_HOSTILE}", tmp_path / "prompt.md"
    rules.write_text("RULES")
    prompt.write_text("PROMPT")
    argv = stdin_command("fake-cli", [], prompt_path=str(prompt), rules_path=str(rules))
    assert _run(argv, tmp_path).endswith("RULES\n\nPROMPT")
    assert not _pwned(tmp_path)


@pytest.mark.usefixtures("fake_cli")
def test_auth_file_is_written_private_and_hidden_from_the_cli_env(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi")
    argv = stdin_command("fake-cli", [], prompt_path=str(prompt), auth_file=".cli/auth.json")
    out = _run(argv, tmp_path, {AUTH_FILE_ENV: '{"token": "s3cret $(touch pwned)"}'})
    auth = tmp_path / "home" / ".cli" / "auth.json"
    assert auth.read_text() == '{"token": "s3cret $(touch pwned)"}'
    assert auth.stat().st_mode & 0o077 == 0
    assert "env=unset" in out
    assert "s3cret" not in out
    assert not _pwned(tmp_path)


@pytest.mark.usefixtures("fake_cli")
def test_auth_file_refreshed_by_the_cli_is_never_overwritten(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi")
    auth = tmp_path / "home" / ".cli" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("refreshed")
    argv = stdin_command("fake-cli", [], prompt_path=str(prompt), auth_file=".cli/auth.json")
    _run(argv, tmp_path, {AUTH_FILE_ENV: "original"})
    assert auth.read_text() == "refreshed"


@pytest.mark.usefixtures("fake_cli")
def test_config_file_is_rewritten_every_session_and_never_follows_a_symlink(
    tmp_path: Path,
) -> None:
    prompt, victim = tmp_path / "prompt.md", tmp_path / "victim"
    prompt.write_text("hi")
    victim.write_text("untouched")
    config = tmp_path / "home" / ".cli" / "settings.json"
    config.parent.mkdir(parents=True)
    config.symlink_to(victim)
    content = '{"limit": 3, "x": "$(touch pwned) %s"}'
    argv = stdin_command(
        "fake-cli", ["--a"], prompt_path=str(prompt), config_file=(".cli/settings.json", content)
    )
    assert _run(argv, tmp_path).startswith("--a\n")
    assert victim.read_text() == "untouched"
    assert not config.is_symlink()
    assert config.read_text() == content
    assert config.stat().st_mode & 0o077 == 0
    assert not _pwned(tmp_path)


@pytest.mark.parametrize("binary", ["", "rm -rf /", "a;b", "$(x)", "-p", "Claude"])
def test_hostile_binary_name_is_refused(binary: str) -> None:
    with pytest.raises(ValueError, match="invalid binary"):
        stdin_command(binary, [], prompt_path="/p")


def test_jsonl_events_skips_garbage(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    lines = ["not json", '{"a": 1}', "[1]", '"s"', "{broken", "\x00\xff", '{"b": 2}']
    path.write_text("\n".join(lines))
    assert list(jsonl_events(path)) == [{"a": 1}, {"b": 2}]
    assert list(jsonl_events(tmp_path / "absent")) == []


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


@pytest.mark.parametrize(("value", "expected"), [(2, 2.0), (0.5, 0.5), (True, 9.0), ("1", 9.0)])
def test_number_rejects_non_numbers(value: object, expected: float) -> None:
    assert number(value, 9.0) == expected


def test_secret_value_strips_and_treats_blank_as_missing() -> None:
    credentials = {"a": SecretStr(" key \n"), "b": SecretStr("   ")}
    assert secret_value(credentials, "a") == "key"
    assert secret_value(credentials, "b") is None
    assert secret_value(credentials, "c") is None
