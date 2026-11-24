import asyncio
from dataclasses import dataclass
from pathlib import Path

from skeleton_crew.domain.job_policy import UNTRUSTED_OUTPUT_CHARS
from skeleton_crew.domain.task_keys import COMMAND_SUFFIX, RepoCommand
from skeleton_crew.domain.text_policy import MAX_UTF8_BYTES_PER_CHAR
from skeleton_crew.pipeline.worker_job import WorkerJob
from skeleton_crew.runtime.task_dirs import next_numbered


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
        file.seek(max(path.stat().st_size - UNTRUSTED_OUTPUT_CHARS * MAX_UTF8_BYTES_PER_CHAR, 0))
        return file.read().decode(errors="replace")[-UNTRUSTED_OUTPUT_CHARS:]


async def run_repo_command(job: WorkerJob, command: str, name: RepoCommand) -> CommandResult:
    transcripts = job.require_sandbox().transcripts
    log_path = await asyncio.to_thread(next_numbered, transcripts, name, COMMAND_SUFFIX)
    # No env is passed: the repo's own commands never see the model credential.
    result = await job.exec_as_agent(
        ["sh", "-c", f"exec 2>&1\n{command}"],
        timeout_s=job.ctx.settings.limits.command_timeout_s,
        stdout_path=log_path,
    )
    return CommandResult(
        result.exit_code, result.timed_out, await asyncio.to_thread(_tail, log_path)
    )
