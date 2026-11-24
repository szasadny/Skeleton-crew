from pathlib import Path
from typing import Any

from skeleton_crew.pipeline.sandbox import INBOX
from skeleton_crew.runtime.containers import WORKSPACE, ContainerSpec, Mount


def container_spec(root: Path, **overrides: Any) -> ContainerSpec:
    values: dict[str, Any] = {
        "name": "agent-1",
        "image": "skeleton-crew-agent:latest",
        "network": "skeleton-crew-agents",
        "mounts": [
            Mount(root / "agent", WORKSPACE, read_only=False),
            Mount(root / "inbox", INBOX, read_only=True),
        ],
        "env": {"HTTPS_PROXY": "http://egress-proxy:3128", "CLAUDE_CODE_OAUTH_TOKEN": "x"},
        "cpus": 1.5,
        "memory_gb": 2.0,
        "pids": 512,
        "labels": {"skeleton-crew.task": "w-1"},
    }
    return ContainerSpec(**(values | overrides))
