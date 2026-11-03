import asyncio
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from skeleton_crew.domain.settings import RuntimeLimits
from skeleton_crew.runtime.containers import ContainerHandle, ContainerRuntime

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
        RuntimeLimits(egress_allowlist=tuple(sorted(normalized)))
    except ValidationError as exc:
        raise ValueError(f"invalid egress domain: {exc.errors()[0]['msg']}") from exc
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
    runtime: ContainerRuntime, domains: Iterable[str], file: Path, proxy_container: str
) -> None:
    content = render_allowlist(domains)
    await asyncio.to_thread(_write_atomic, file, content)
    handle = ContainerHandle(id=proxy_container, name=proxy_container)
    result = await runtime.exec(
        handle,
        RECONFIGURE_ARGV,
        env={},
        cwd="/",
        user="root",
        timeout_s=RECONFIGURE_TIMEOUT_S,
    )
    if result.exit_code != 0 or result.timed_out:
        detail = result.stderr.decode(errors="replace").strip()[-300:]
        raise EgressError(f"egress proxy reconfigure failed ({result.exit_code}): {detail}")
