import asyncio
from dataclasses import dataclass
from pathlib import Path

from skeleton_crew.pipeline.sessions import next_numbered
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.runtime.containers import ExecRequest

OUTPUT_TAIL_CHARS = 20_000


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    timed_out: bool
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def status(self) -> str:
        return "timed out" if self.timed_out else f"exit {self.exit_code}"


def _tail(path: Path) -> str:
    with path.open("rb") as file:
        file.seek(max(path.stat().st_size - OUTPUT_TAIL_CHARS * 4, 0))
        return file.read().decode(errors="replace")[-OUTPUT_TAIL_CHARS:]


async def run_repo_command(job: WorkerJob, command: str, name: str) -> CommandResult:
    if job.sandbox is None:
        raise RuntimeError("worker container is not running")
    log_path = await asyncio.to_thread(next_numbered, job.sandbox.transcripts, name, ".log")
    # No env is passed: the repo's own commands never see the model credential.
    request = ExecRequest.as_agent(
        ["sh", "-c", f"exec 2>&1\n{command}"],
        timeout_s=job.ctx.settings.limits.command_timeout_s,
        stdout_path=log_path,
    )
    result = await job.ctx.deps.runtime.exec(job.sandbox.handle, request)
    return CommandResult(
        result.exit_code, result.timed_out, await asyncio.to_thread(_tail, log_path)
    )
