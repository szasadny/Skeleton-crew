import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import docker.errors
import docker.types
import pytest
from docker.api.client import APIClient
from docker.client import DockerClient

from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.runtime import containers
from skeleton_crew.runtime.containers import (
    MANAGED_LABEL,
    AgentContainerSpec,
    ContainerHandle,
    DockerRuntime,
    Mount,
    timeout_argv,
)

HANDLE = ContainerHandle(id="c1", name="agent-1")


def make_spec(tmp_path: Path, **overrides: Any) -> AgentContainerSpec:
    values: dict[str, Any] = {
        "name": "agent-1",
        "image": "skeleton-crew-agent:latest",
        "network": "skeleton-crew-agents",
        "mounts": [
            Mount(tmp_path / "agent", "/workspace", read_only=False),
            Mount(tmp_path / "inbox", "/skeleton-in", read_only=True),
        ],
        "env": {"HTTPS_PROXY": "http://egress-proxy:3128", "CLAUDE_CODE_OAUTH_TOKEN": "x"},
        "cpus": 1.5,
        "memory_gb": 2.0,
        "pids": 512,
        "labels": {"skeleton-crew.task": "w-1"},
    }
    values.update(overrides)
    return AgentContainerSpec(**values)


@pytest.fixture
def client() -> MagicMock:
    mock = MagicMock(spec=DockerClient)
    mock.api = MagicMock(spec=APIClient)
    return mock


class Stream:
    def __init__(self, chunks: list[tuple[bytes | None, bytes | None]]) -> None:
        self._chunks = chunks
        self.closed = False
        self._response = MagicMock()

    def __iter__(self) -> Iterator[tuple[bytes | None, bytes | None]]:
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


def script_exec(client: MagicMock, stream: Any, exit_code: int = 0) -> None:
    client.api.exec_create.return_value = {"Id": "e1"}
    client.api.exec_start.return_value = stream
    client.api.exec_inspect.return_value = {"Running": False, "ExitCode": exit_code}


async def test_start_builds_hardened_container(tmp_path: Path, client: MagicMock) -> None:
    client.containers.run.return_value.id = "abc"
    handle = await DockerRuntime(client).start(make_spec(tmp_path))

    assert handle == ContainerHandle(id="abc", name="agent-1")
    kwargs = client.containers.run.call_args.kwargs
    assert client.containers.run.call_args.args == ("skeleton-crew-agent:latest",)
    assert kwargs["runtime"] == "sysbox-runc"
    assert kwargs["privileged"] is False
    assert kwargs["detach"] is True
    assert kwargs["auto_remove"] is False
    assert kwargs["network"] == "skeleton-crew-agents"
    assert kwargs["nano_cpus"] == 1_500_000_000
    assert kwargs["mem_limit"] == "2048m"
    assert kwargs["pids_limit"] == 512
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["labels"] == {"skeleton-crew.task": "w-1", MANAGED_LABEL: "true"}
    assert kwargs["mounts"] == [
        docker.types.Mount("/workspace", str(tmp_path / "agent"), type="bind", read_only=False),
        docker.types.Mount("/skeleton-in", str(tmp_path / "inbox"), type="bind", read_only=True),
    ]
    assert "volumes" not in kwargs


async def test_managed_label_cannot_be_overridden(tmp_path: Path, client: MagicMock) -> None:
    client.containers.run.return_value.id = "abc"
    await DockerRuntime(client).start(make_spec(tmp_path, labels={MANAGED_LABEL: "false"}))
    assert client.containers.run.call_args.kwargs["labels"][MANAGED_LABEL] == "true"


@pytest.mark.parametrize(
    ("host", "target"),
    [
        (Path("/var/run/docker.sock"), "/var/run/docker.sock"),
        (Path("/run/docker.sock"), "/data"),
        (Path("/srv/elsewhere/docker.sock"), "/sock"),
        (Path("/srv/data"), "/var/run/docker.sock"),
    ],
)
async def test_docker_socket_mount_is_refused(
    tmp_path: Path, client: MagicMock, host: Path, target: str
) -> None:
    spec = make_spec(tmp_path, mounts=[Mount(host, target, read_only=True)])
    with pytest.raises(PolicyViolation, match="docker socket"):
        await DockerRuntime(client).start(spec)
    client.containers.run.assert_not_called()


async def test_symlink_to_docker_socket_is_refused(tmp_path: Path, client: MagicMock) -> None:
    link = tmp_path / "innocent"
    link.symlink_to("/var/run/docker.sock")
    spec = make_spec(tmp_path, mounts=[Mount(link, "/data", read_only=True)])
    with pytest.raises(PolicyViolation, match="docker socket"):
        await DockerRuntime(client).start(spec)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GH_TOKEN", "x"),
        ("GITHUB_TOKEN", "x"),
        ("github_token", "x"),
        ("MY_GITHUB_APP_TOKEN", "x"),
        ("GH_ENTERPRISE_TOKEN", "x"),
        ("GIT_CONFIG_VALUE_0", "Authorization: Basic abc"),
        ("INNOCENT", "ghs_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("INNOCENT", "github_pat_abc"),
        ("REMOTE", "https://x-access-token:abc@github.com/o/r"),
    ],
)
async def test_github_credentials_never_enter_a_container(
    tmp_path: Path, client: MagicMock, key: str, value: str
) -> None:
    runtime = DockerRuntime(client)
    with pytest.raises(PolicyViolation, match="GitHub credential"):
        await runtime.start(make_spec(tmp_path, env={key: value}))
    with pytest.raises(PolicyViolation, match="GitHub credential"):
        await runtime.exec(HANDLE, ["true"], env={key: value}, cwd="/", user="agent", timeout_s=5)
    client.containers.run.assert_not_called()
    client.api.exec_create.assert_not_called()


def test_timeout_wrapper_sends_sigint_then_kills() -> None:
    assert timeout_argv(["claude", "-p"], 7.2) == [
        "timeout", "-s", "INT", "-k", str(containers.KILL_GRACE_S), "8", "claude", "-p",
    ]  # fmt: skip
    assert timeout_argv(["x"], 0.01)[5] == "1"


async def test_exec_streams_stdout_to_file_with_cap(tmp_path: Path, client: MagicMock) -> None:
    stream = Stream([(b"0123", None), (None, b"warn"), (b"456789", b"!")])
    script_exec(client, stream)
    out = tmp_path / "out.jsonl"

    result = await DockerRuntime(client).exec(
        HANDLE, ["claude", "-p"], env={"A": "1"}, cwd="/workspace", user="agent",
        timeout_s=60, stdout_path=out, max_stdout_bytes=7,
    )  # fmt: skip

    assert out.read_bytes() == b"0123456"
    assert result.exit_code == 0
    assert result.stdout_path == out
    assert result.stdout == b""
    assert result.stderr == b"warn!"
    assert result.timed_out is False
    assert stream.closed
    stream._response.close.assert_called_once()
    args, kwargs = client.api.exec_create.call_args
    assert args == ("c1", timeout_argv(["claude", "-p"], 60))
    assert kwargs["user"] == "agent"
    assert kwargs["workdir"] == "/workspace"
    assert kwargs["environment"] == {"A": "1"}
    assert client.api.exec_start.call_args.kwargs == {"stream": True, "demux": True}


async def test_exec_keeps_bounded_stdout_and_stderr_in_memory(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(containers, "STDERR_TAIL_BYTES", 4)
    script_exec(client, Stream([(b"abcdef", b"123456")]), exit_code=3)
    result = await DockerRuntime(client).exec(
        HANDLE, ["x"], env={}, cwd="/", user="agent", timeout_s=5, max_stdout_bytes=3
    )
    assert result.stdout == b"abc"
    assert result.stderr == b"3456"
    assert result.exit_code == 3


async def test_exec_exit_124_is_timed_out(client: MagicMock) -> None:
    script_exec(client, Stream([]), exit_code=124)
    result = await DockerRuntime(client).exec(
        HANDLE, ["x"], env={}, cwd="/", user="agent", timeout_s=5
    )
    assert result.timed_out is True


async def test_exec_controller_deadline_marks_timed_out(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(containers, "KILL_GRACE_S", 0)
    monkeypatch.setattr(containers, "_CONTROLLER_TIMEOUT_MARGIN_S", 0.0)
    release = threading.Event()

    def hanging() -> Iterator[tuple[bytes | None, bytes | None]]:
        release.wait(5)
        yield (b"late", None)

    stream = MagicMock()
    stream.__iter__.side_effect = hanging
    script_exec(client, stream)
    try:
        result = await DockerRuntime(client).exec(
            HANDLE, ["sleep", "100"], env={}, cwd="/", user="agent", timeout_s=0.2
        )
    finally:
        release.set()
    assert result.timed_out is True
    assert result.exit_code == -1


async def test_remove_ignores_missing_container(client: MagicMock) -> None:
    client.containers.get.side_effect = docker.errors.NotFound("gone")
    await DockerRuntime(client).remove(HANDLE)


async def test_remove_forces_removal(client: MagicMock) -> None:
    await DockerRuntime(client).remove(HANDLE)
    client.containers.get.assert_called_once_with("c1")
    client.containers.get.return_value.remove.assert_called_once_with(force=True, v=True)


async def test_remove_all_managed_filters_by_label(client: MagicMock) -> None:
    gone = MagicMock()
    gone.remove.side_effect = docker.errors.NotFound("raced")
    client.containers.list.return_value = [MagicMock(), gone, MagicMock()]
    assert await DockerRuntime(client).remove_all_managed() == 2
    client.containers.list.assert_called_once_with(
        all=True, filters={"label": f"{MANAGED_LABEL}=true"}
    )


def healthy(client: MagicMock) -> None:
    client.info.return_value = {"Runtimes": {"runc": {}, "sysbox-runc": {}}}
    client.networks.get.return_value.attrs = {"Internal": True}
    client.containers.get.return_value.status = "running"


async def health(client: MagicMock) -> list[str]:
    return await DockerRuntime(client).health(image="img", network="net", proxy_container="proxy")


async def test_health_ok(client: MagicMock) -> None:
    healthy(client)
    assert await health(client) == []
    client.images.get.assert_called_once_with("img")


async def test_health_daemon_down(client: MagicMock) -> None:
    client.ping.side_effect = docker.errors.DockerException("down")
    assert await health(client) == ["docker daemon is unreachable"]


async def test_health_reports_every_problem(client: MagicMock) -> None:
    healthy(client)
    client.info.return_value = {"Runtimes": {"runc": {}}}
    client.images.get.side_effect = docker.errors.ImageNotFound("no")
    client.networks.get.return_value.attrs = {"Internal": False}
    client.containers.get.return_value.status = "exited"
    assert await health(client) == [
        "docker runtime sysbox-runc is not installed",
        "agent image img is missing",
        "network net is not internal",
        "egress proxy proxy is not running",
    ]


async def test_health_missing_network_and_proxy(client: MagicMock) -> None:
    healthy(client)
    client.networks.get.side_effect = docker.errors.NotFound("no")
    client.containers.get.side_effect = docker.errors.NotFound("no")
    assert await health(client) == ["network net is missing", "egress proxy proxy is missing"]


async def test_health_network_without_internal_flag_fails_closed(client: MagicMock) -> None:
    healthy(client)
    client.networks.get.return_value.attrs = {}
    assert await health(client) == ["network net is not internal"]
