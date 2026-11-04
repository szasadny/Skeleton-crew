import asyncio
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from skeleton_crew.core.logging import redact

HARDENING: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.allow=never",
)
_PROTOCOL_RE = re.compile(r"^[a-z][a-z0-9+-]{0,31}$")
_ERROR_CHARS = 500


class GitCommandError(Exception):
    pass


def git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    base = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.devnull,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    return {**(extra or {}), **base}


def git_argv(args: Sequence[str], *, protocols: Sequence[str] = ()) -> list[str]:
    allow: list[str] = []
    for protocol in protocols:
        if not _PROTOCOL_RE.fullmatch(protocol):
            raise ValueError(f"invalid git protocol: {protocol!r}")
        allow += ["-c", f"protocol.{protocol}.allow=always"]
    return ["git", *HARDENING, *allow, *args]


def _failure(args: Sequence[str], stderr: bytes, secrets: Sequence[str]) -> GitCommandError:
    detail = redact(stderr.decode(errors="replace").strip(), secrets)
    return GitCommandError(f"git {args[0]} failed: {detail[-_ERROR_CHARS:]}")


def run_git_sync(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    stdin: bytes | None = None,
    stdout: IO[bytes] | None = None,
    env: Mapping[str, str] | None = None,
    exit_codes: frozenset[int] = frozenset({0}),
) -> bytes:
    completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        git_argv(args),
        cwd=cwd,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=git_env(env),
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode not in exit_codes:
        raise _failure(args, completed.stderr, ())
    return completed.stdout if stdout is None else b""


async def run_git(
    args: Sequence[str],
    *,
    cwd: Path | None,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    protocols: Sequence[str] = (),
    secrets: Sequence[str] = (),
) -> str:
    proc = await asyncio.create_subprocess_exec(
        *git_argv(args, protocols=protocols),
        cwd=cwd,
        env=git_env(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitCommandError(f"git {args[0]} timed out") from None
    if proc.returncode != 0:
        raise _failure(args, stderr, secrets)
    return stdout.decode(errors="replace")
