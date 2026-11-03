import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence

GITLEAKS_ARGV = (
    "gitleaks",
    "stdin",
    "--no-banner",
    "--redact",
    "--report-format",
    "json",
    "--report-path",
    "/dev/stdout",
    "--exit-code",
    "0",
)
SCAN_TIMEOUT_S = 300.0

Runner = Callable[[Sequence[str], bytes], Awaitable[tuple[int, bytes, bytes]]]


class SecretScanError(Exception):
    pass


async def _run(argv: Sequence[str], stdin: bytes) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), SCAN_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout, stderr


def _file_at_line(patch_lines: Sequence[bytes], line: int) -> str:
    for header in reversed(patch_lines[: max(line, 0)]):
        if header.startswith(b"diff --git "):
            _, _, new = header.partition(b" b/")
            return new.decode(errors="replace") or "?"
    return "?"


def _describe(finding: object, patch_lines: Sequence[bytes]) -> str:
    if not isinstance(finding, dict):
        raise SecretScanError("unexpected gitleaks finding format")
    rule = str(finding.get("RuleID") or "unknown-rule")
    line = finding.get("StartLine")
    line_no = line if isinstance(line, int) else 0
    file = str(finding.get("File") or "") or _file_at_line(patch_lines, line_no)
    return f"{rule} in {file}"


async def scan_patch(patch: bytes, *, runner: Runner = _run) -> list[str]:
    try:
        exit_code, stdout, stderr = await runner(GITLEAKS_ARGV, patch)
    except FileNotFoundError as exc:
        raise SecretScanError("gitleaks is not installed") from exc
    except TimeoutError as exc:
        raise SecretScanError("gitleaks timed out") from exc
    if exit_code != 0:
        detail = stderr.decode(errors="replace").strip()[-300:]
        raise SecretScanError(f"gitleaks failed ({exit_code}): {detail}")
    try:
        findings = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SecretScanError("gitleaks report is not valid JSON") from exc
    if not isinstance(findings, list):
        raise SecretScanError("gitleaks report is not a list")
    patch_lines = patch.split(b"\n")
    return sorted({_describe(finding, patch_lines) for finding in findings})
