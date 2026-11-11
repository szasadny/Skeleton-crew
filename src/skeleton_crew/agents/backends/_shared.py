import json
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import SecretStr

AUTH_FILE_ENV = "SC_AUTH_FILE"
_BINARY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_REASON_RE = re.compile(r"^[a-z_]{1,40}$")
# Ids are later passed back to the CLI, so they must never look like an option.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def safe_session_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _SESSION_ID_RE.match(value) else None


def safe_reason(value: Any) -> str | None:
    return value if isinstance(value, str) and _REASON_RE.match(value) else None


def jsonl_events(stdout_path: Path) -> Iterator[dict[str, Any]]:
    try:
        with stdout_path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError, RecursionError:
                    continue
                if isinstance(event, dict):
                    yield event
    except FileNotFoundError:
        return


def secret_value(credentials: Mapping[str, SecretStr], name: str) -> str | None:
    secret = credentials.get(name)
    value = secret.get_secret_value().strip() if secret is not None else ""
    return value or None


def stdin_command(
    binary: str,
    args: Sequence[str],
    *,
    prompt_path: str,
    rules_path: str | None = None,
    auth_file: str | None = None,
    config_file: tuple[str, str] | None = None,
) -> list[str]:
    if not _BINARY_RE.fullmatch(binary):
        raise ValueError(f"invalid binary name: {binary}")
    # Paths travel as positional arguments, never inside the script, so they cannot inject.
    script, values = ['prompt="$1"; shift;'], [prompt_path]
    if rules_path is not None:
        script.append('rules="$1"; shift;')
        values.append(rules_path)
    if auth_file is not None or config_file is not None:
        script.append("umask 077;")
    if auth_file is not None:
        # Never overwrite: the CLI may have refreshed the login during an earlier session.
        script.append(
            'auth="$HOME/$1"; shift; mkdir -p "$(dirname "$auth")";'
            f' [ -e "$auth" ] || printf "%s" "${AUTH_FILE_ENV}" > "$auth"; unset {AUTH_FILE_ENV};'
        )
        values.append(auth_file)
    if config_file is not None:
        # Always rewritten, after removing whatever the agent may have left there (e.g. a symlink).
        script.append(
            'config="$HOME/$1"; mkdir -p "$(dirname "$config")" && rm -rf "$config"'
            ' && printf "%s" "$2" > "$config" || exit 1; shift 2;'
        )
        values.extend(config_file)
    if rules_path is None:
        script.append(f'exec {binary} "$@" < "$prompt"')
    else:
        script.append(f'{{ cat "$rules"; printf "\\n\\n"; cat "$prompt"; }} | exec {binary} "$@"')
    # On stdin, because Linux caps a single argument at 128 KiB and prompts can be larger.
    return ["sh", "-c", " ".join(script), "sh", *values, *args]
