import asyncio
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from skeleton_crew.domain.settings import check_domains
from skeleton_crew.runtime.containers import (
    ContainerHandle,
    ContainerRuntime,
    ExecRequest,
    SandboxTarget,
    error_tail,
)

PLACEHOLDER_DOMAIN = ".invalid"
RECONFIGURE_ARGV = ("squid", "-k", "reconfigure")
RECONFIGURE_TIMEOUT_S = 30.0


class EgressError(Exception):
    pass


def _covered(entry: str, wildcards: set[str]) -> bool:
    name = entry.removeprefix(".")
    return any(
        (name == base and entry != f".{base}") or name.endswith(f".{base}") for base in wildcards
    )


def render_allowlist(domains: Iterable[str]) -> str:
    normalized = {domain.strip().lower() for domain in domains}
    try:
        check_domains(tuple(sorted(normalized)))
    except ValueError as exc:
        raise ValueError(f"invalid egress domain: {exc}") from exc
    wildcards = {d.removeprefix(".") for d in normalized if d.startswith(".")}
    # Squid rejects entries that a ".domain" wildcard already covers.
    kept = sorted(d for d in normalized if not _covered(d, wildcards))
    return "\n".join(kept or [PLACEHOLDER_DOMAIN]) + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def apply_allowlist(
    runtime: ContainerRuntime, domains: Iterable[str], target: SandboxTarget
) -> None:
    content = render_allowlist(domains)
    await asyncio.to_thread(_write_atomic, target.allowlist_path, content)
    handle = ContainerHandle(id=target.proxy_container, name=target.proxy_container)
    request = ExecRequest.as_root(RECONFIGURE_ARGV, timeout_s=RECONFIGURE_TIMEOUT_S)
    result = await runtime.exec(handle, request)
    if result.exit_code != 0 or result.timed_out:
        detail = error_tail(result.stderr)
        raise EgressError(f"egress proxy reconfigure failed ({result.exit_code}): {detail}")
