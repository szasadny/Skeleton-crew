import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import SecretStr

from skeleton_crew.agents.backends._shared import AUTH_FILE_ENV

HOSTILE_PROMPT = "Fix it; $(rm -rf /) 'q' \"qq\"\nsecond line"
LARGE_PROMPT = "x" * 300_000 + " $(touch pwned)"
ECHO_SCRIPT = f'printf "%s\\n" "$*"; printf "env=%s\\n" "${{{AUTH_FILE_ENV}:-unset}}"; cat\n'


def write_jsonl(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "stdout.jsonl"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def credentials(**values: SecretStr | None) -> dict[str, SecretStr]:
    return {name: value for name, value in values.items() if value is not None}


def fake_cli(tmp_path: Path, name: str, script: str = ECHO_SCRIPT) -> None:
    if shutil.which("sh") is None:
        pytest.skip("sh not installed")
    cli = tmp_path / "bin" / name
    cli.parent.mkdir(exist_ok=True)
    cli.write_text(f"#!/bin/sh\n{script}")
    cli.chmod(0o755)


def run_argv(argv: list[str], tmp_path: Path, env: Mapping[str, str] | None = None) -> str:
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


def pwned(tmp_path: Path) -> bool:
    return any((tmp_path / name).exists() for name in ("pwned", "pwned2", "pwned3"))
