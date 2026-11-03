import argparse
import asyncio
import getpass
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime

import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from skeleton_crew.agents.base import AgentBackend
from skeleton_crew.agents.registry import backend_for
from skeleton_crew.core.config import BootstrapConfig, get_config
from skeleton_crew.core.db import make_engine, make_sessionmaker, run_migrations
from skeleton_crew.core.logging import configure_logging
from skeleton_crew.core.security import hash_password
from skeleton_crew.domain.settings import AgentBackendName, AppSettings
from skeleton_crew.github.gateway import Gateway, GitHubGateway, make_github_client
from skeleton_crew.github.write_guard import AuditSink
from skeleton_crew.pipeline.deps import PipelineDeps, audit_sink
from skeleton_crew.pipeline.run_controller import RunController
from skeleton_crew.pipeline.run_setup import make_backends, preflight_problems
from skeleton_crew.runtime.containers import DockerRuntime
from skeleton_crew.runtime.secret_scan import scan_patch
from skeleton_crew.scheduler.scheduler import RunScheduler
from skeleton_crew.store import settings_store
from skeleton_crew.web.app import create_app


def _now() -> datetime:
    return datetime.now(UTC)


async def _open_database(config: BootstrapConfig) -> AsyncEngine:
    for directory in (config.data_dir, config.tasks_dir, config.egress_allowlist_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config.db_path)
    await run_migrations(engine)
    return engine


async def _build_deps(
    config: BootstrapConfig, sessionmaker: async_sessionmaker[AsyncSession]
) -> PipelineDeps:
    private_key = config.github_app_private_key_path.read_text()
    client, token_provider = await make_github_client(
        config.github_app_id, private_key, config.github_app_installation_id, config.repo
    )

    def gateway_factory(settings: AppSettings, audit: AuditSink) -> Gateway:
        return GitHubGateway(
            client=client,
            repo=config.repo,
            labels=settings.labels,
            base_branch=settings.repo.base_branch,
            dry_run=settings.dry_run,
            audit=audit,
            max_comment_chars=settings.limits.max_comment_chars,
            token_provider=token_provider,
        )

    def backend_factory(name: AgentBackendName, settings: AppSettings) -> AgentBackend:
        return backend_for(name, config=config, auth_mode=settings.claude_auth_mode)

    return PipelineDeps(
        config=config,
        sessionmaker=sessionmaker,
        gateway_factory=gateway_factory,
        runtime=DockerRuntime(),
        backend_factory=backend_factory,
        secret_scanner=scan_patch,
        clock=_now,
        sleep=asyncio.sleep,
    )


async def _serve(config: BootstrapConfig) -> None:
    async with AsyncExitStack() as stack:
        engine = await _open_database(config)
        stack.push_async_callback(engine.dispose)
        sessionmaker = make_sessionmaker(engine)
        controller = RunController(await _build_deps(config, sessionmaker))
        await controller.reconcile_on_startup()

        async def scheduled_run() -> int | None:
            return await controller.start("schedule")

        scheduler = RunScheduler(scheduled_run)
        app = create_app(
            config=config, sessionmaker=sessionmaker, controller=controller, scheduler=scheduler
        )
        # Only Caddy can reach the controller (it is not published), so its forwarded IP is trusted.
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                proxy_headers=True,
                forwarded_allow_ips="*",
                log_config=None,
            )
        )
        await server.serve()
        if controller.is_running:
            await controller.kill()


async def _preflight(config: BootstrapConfig) -> list[str]:
    engine = await _open_database(config)
    try:
        sessionmaker = make_sessionmaker(engine)
        deps = await _build_deps(config, sessionmaker)
        async with sessionmaker() as session:
            settings = await settings_store.load_settings(session)
        gateway = deps.gateway_factory(settings, audit_sink(sessionmaker, None))
        return await preflight_problems(deps, settings, gateway, make_backends(deps, settings))
    finally:
        await engine.dispose()


def _hash_password() -> int:
    password = getpass.getpass("UI password: ")
    if len(password) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    # Single quotes keep docker compose from interpolating the `$` signs of the argon2 hash.
    print(f"SC_UI_PASSWORD_HASH='{hash_password(password)}'")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skeleton-crew")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the web UI, scheduler and controller")
    commands.add_parser("hash-password", help="print an argon2 hash for SC_UI_PASSWORD_HASH")
    commands.add_parser("preflight", help="check repository, runtime and agent credentials")
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        return _hash_password()
    config = get_config()
    configure_logging(config.secret_values())
    if args.command == "serve":
        asyncio.run(_serve(config))
        return 0
    problems = asyncio.run(_preflight(config))
    for problem in problems:
        print(f"- {problem}")
    print("Preflight OK." if not problems else f"{len(problems)} problem(s).")
    return 1 if problems else 0
