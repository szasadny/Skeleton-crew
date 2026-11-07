import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import docker.errors
import pytest
from docker.client import DockerClient, from_env
from docker.models.images import Image

from skeleton_crew.domain.branch_policy import BRANCH_PREFIX
from skeleton_crew.runtime.containers import (
    READY_FILE,
    SANDBOX_RUNTIME,
    AgentContainerSpec,
    ContainerHandle,
    ContainerStartError,
    DockerRuntime,
    Mount,
)

pytestmark = pytest.mark.docker

IMAGE = "skeleton-crew-agent:latest"
RUNNER_IMAGE = "skeleton-crew-runner:latest"
HOOK = "/usr/local/bin/job-started.sh"


@pytest.fixture
def client() -> Iterator[DockerClient]:
    docker_client = from_env()
    if SANDBOX_RUNTIME not in (docker_client.info().get("Runtimes") or {}):
        pytest.fail("the docker tests require the sysbox-runc runtime")
    try:
        docker_client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        pytest.fail(f"build {IMAGE} before running the docker tests")
    yield docker_client
    docker_client.close()


@pytest.fixture
def network(client: DockerClient) -> Iterator[str]:
    name = f"sc-test-{uuid.uuid4().hex[:8]}"
    net = client.networks.create(name, internal=True)
    yield name
    net.remove()


@pytest.fixture
async def started(
    client: DockerClient, network: str, tmp_path: Path
) -> AsyncIterator[tuple[DockerRuntime, ContainerHandle]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi\n")
    runtime = DockerRuntime(client)
    handle = await runtime.start(
        AgentContainerSpec(
            name=f"sc-test-{uuid.uuid4().hex[:8]}", image=IMAGE, network=network,
            mounts=[Mount(workspace, "/workspace", read_only=True)], env={"SC_TEST": "1"},
            cpus=1, memory_gb=1, pids=256, labels={"skeleton-crew.test": "true"},
        )
    )  # fmt: skip
    yield runtime, handle
    await runtime.remove(handle)


async def test_exec_runs_as_agent_in_sysbox(
    started: tuple[DockerRuntime, ContainerHandle], client: DockerClient
) -> None:
    runtime, handle = started
    result = await runtime.exec(
        handle, ["sh", "-c", 'id -un; cat /workspace/hello.txt; echo "$SC_TEST"'],
        env={}, cwd="/workspace", user="agent", timeout_s=30,
    )  # fmt: skip
    assert result.exit_code == 0
    assert result.stdout.decode().split() == ["agent", "hi", "1"]
    attrs = client.containers.get(handle.id).attrs["HostConfig"]
    assert attrs["Runtime"] == SANDBOX_RUNTIME
    assert attrs["Privileged"] is False


async def test_start_returns_only_after_the_entrypoint_is_ready(
    started: tuple[DockerRuntime, ContainerHandle],
) -> None:
    runtime, handle = started
    result = await runtime.exec(
        handle, ["test", "-f", READY_FILE], env={}, cwd="/", user="agent", timeout_s=30
    )
    assert result.exit_code == 0


@pytest.mark.parametrize("cli", ["claude", "codex", "gemini", "opencode"])
async def test_every_agent_cli_is_installed(
    started: tuple[DockerRuntime, ContainerHandle], cli: str
) -> None:
    runtime, handle = started
    result = await runtime.exec(
        handle, [cli, "--version"], env={}, cwd="/workspace", user="agent", timeout_s=60
    )
    assert result.exit_code == 0


async def test_workspace_mount_is_read_only(started: tuple[DockerRuntime, ContainerHandle]) -> None:
    runtime, handle = started
    result = await runtime.exec(
        handle, ["touch", "/workspace/x"], env={}, cwd="/", user="agent", timeout_s=30
    )
    assert result.exit_code != 0


async def test_exec_timeout_interrupts_the_command(
    started: tuple[DockerRuntime, ContainerHandle],
) -> None:
    runtime, handle = started
    result = await runtime.exec(handle, ["sleep", "60"], env={}, cwd="/", user="agent", timeout_s=1)
    assert result.timed_out is True


async def test_container_has_no_route_to_the_internet(
    started: tuple[DockerRuntime, ContainerHandle],
) -> None:
    runtime, handle = started
    result = await runtime.exec(
        handle, ["curl", "-sS", "--max-time", "5", "https://example.com"],
        env={}, cwd="/", user="agent", timeout_s=30,
    )  # fmt: skip
    assert result.exit_code != 0


async def test_remove_all_managed_cleans_up(started: tuple[DockerRuntime, ContainerHandle]) -> None:
    runtime, _ = started
    assert await runtime.remove_all_managed() >= 1
    await asyncio.sleep(0)


@pytest.fixture
def runner_image(client: DockerClient) -> Image:
    try:
        return client.images.get(RUNNER_IMAGE)
    except docker.errors.ImageNotFound:
        pytest.fail(f"build {RUNNER_IMAGE} before running the docker tests")


async def test_unregistered_runner_never_becomes_ready(
    client: DockerClient, network: str, tmp_path: Path, runner_image: Image
) -> None:
    config = tmp_path / "runner-1"
    config.mkdir()
    label = f"sc-test-{uuid.uuid4().hex[:8]}"
    runtime = DockerRuntime(client)
    spec = AgentContainerSpec(
        name=label,
        image=RUNNER_IMAGE,
        network=network,
        mounts=[Mount(config, "/runner-config", read_only=True)],
        env={},
        cpus=1,
        memory_gb=1,
        pids=256,
        labels={"skeleton-crew.test": label},
    )

    with pytest.raises(ContainerStartError, match="stopped during startup"):
        await runtime.start(spec)

    assert await runtime.list_labeled("skeleton-crew.test", label) == []


def test_runner_image_refuses_non_skeleton_jobs(client: DockerClient, runner_image: Image) -> None:
    assert f"ACTIONS_RUNNER_HOOK_JOB_STARTED={HOOK}" in runner_image.attrs["Config"]["Env"]

    def hook(branch: str) -> None:
        env = {"SC_BRANCH_PREFIX": BRANCH_PREFIX, "GITHUB_REF_NAME": branch}
        client.containers.run(RUNNER_IMAGE, entrypoint=HOOK, environment=env, remove=True)

    hook("skeleton/1-x")
    with pytest.raises(docker.errors.ContainerError):
        hook("main")
