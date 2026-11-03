import asyncio
import shutil

from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.runtime.workspace import TaskPaths, make_readonly_copy


async def readonly_checkout(ctx: RunContext, paths: TaskPaths) -> None:
    sha = await ctx.gateway.clone(paths.controller_repo)
    await asyncio.to_thread(make_readonly_copy, paths.controller_repo, paths.agent_repo, sha)
    await asyncio.to_thread(shutil.rmtree, paths.controller_repo, ignore_errors=True)


def remove_checkout(paths: TaskPaths) -> None:
    for path in (paths.agent_repo, paths.inbox, paths.controller_repo):
        shutil.rmtree(path, ignore_errors=True)
