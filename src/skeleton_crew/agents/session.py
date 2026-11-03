import logging
import os
from pathlib import Path

from skeleton_crew.agents.base import (
    KILL_AFTER_S,
    TIMEOUT_EXIT_CODE,
    AgentBackend,
    Executor,
    SessionOutcome,
    SessionRequest,
    failed_outcome,
)
from skeleton_crew.core.logging import redact

_EXEC_MARGIN_S = 30
_STDERR_LOG_CHARS = 2000

logger = logging.getLogger(__name__)


def _redact_file(path: Path, secrets: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        cleaned = redact(text, secrets)
        if cleaned != text:
            tmp = path.with_name(path.name + ".redacting")
            tmp.write_text(cleaned, encoding="utf-8")
            os.replace(tmp, path)
    except FileNotFoundError:
        return
    except OSError:
        # An unredacted transcript may hold the credential, so it must not survive.
        logger.exception("could not redact transcript %s, deleting it", path)
        path.unlink(missing_ok=True)


async def run_session(
    backend: AgentBackend, executor: Executor, req: SessionRequest, transcript_path: Path
) -> SessionOutcome:
    env = backend.env()
    secrets = list(env.values())
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = await executor(
            backend.command(req),
            env=env,
            cwd=req.cwd,
            timeout_s=req.timeout_s + KILL_AFTER_S + _EXEC_MARGIN_S,
            stdout_path=transcript_path,
        )
    except Exception as exc:
        logger.exception("%s session for %s could not be executed", backend.name, req.role)
        _redact_file(transcript_path, secrets)
        return failed_outcome(f"executor failed: {type(exc).__name__}")
    if result.exit_code != 0 and result.stderr_tail:
        logger.warning(
            "%s session for %s exited %d: %s",
            backend.name,
            req.role,
            result.exit_code,
            redact(result.stderr_tail[-_STDERR_LOG_CHARS:], secrets),
        )
    _redact_file(transcript_path, secrets)
    timed_out = result.timed_out or result.exit_code == TIMEOUT_EXIT_CODE
    try:
        return backend.parse(transcript_path, result.exit_code, timed_out)
    except Exception:
        logger.exception("%s output for %s could not be parsed", backend.name, req.role)
        return failed_outcome("output could not be parsed", timed_out=timed_out)
