from collections.abc import Iterator

import pytest

from skeleton_crew import cli
from skeleton_crew.core.security import verify_password


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    replies: Iterator[str] = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(replies))


def test_hash_password_prints_quoted_env_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, "correct horse battery", "correct horse battery")
    assert cli.main(["hash-password"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("SC_UI_PASSWORD_HASH='$argon2")
    assert line.endswith("'")
    assert verify_password(line.split("=", 1)[1].strip("'"), "correct horse battery")


@pytest.mark.parametrize(
    "answers", [("short", "short"), ("long enough password", "different password")]
)
def test_hash_password_rejects_weak_or_mismatched(
    monkeypatch: pytest.MonkeyPatch, answers: tuple[str, str]
) -> None:
    _answers(monkeypatch, *answers)
    assert cli.main(["hash-password"]) == 1


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["merge"])
