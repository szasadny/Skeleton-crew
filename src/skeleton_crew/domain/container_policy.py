from collections.abc import Mapping
from pathlib import PurePosixPath

from skeleton_crew.domain.errors import PolicyViolation

_DOCKER_SOCKET_PATHS = frozenset({"/var/run/docker.sock", "/run/docker.sock"})
_GITHUB_TOKEN_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")


def _is_github_credential(key: str, value: str) -> bool:
    upper = key.upper()
    if "TOKEN" in upper and ("GITHUB" in upper or upper.startswith("GH_")):
        return True
    if upper.startswith("GIT_CONFIG_"):
        return True
    lowered = value.lower()
    return value.startswith(_GITHUB_TOKEN_PREFIXES) or "x-access-token" in lowered


def check_env(env: Mapping[str, str]) -> None:
    for key, value in env.items():
        if _is_github_credential(key, value):
            raise PolicyViolation(f"GitHub credential in container env: {key}")


def _is_docker_socket(path: str) -> bool:
    return PurePosixPath(path).name == "docker.sock" or path in _DOCKER_SOCKET_PATHS


def check_mount(host_path: str, resolved_host_path: str, container_path: str) -> None:
    if _is_docker_socket(host_path) or _is_docker_socket(resolved_host_path):
        raise PolicyViolation("docker socket must never be mounted into a container")
    if _is_docker_socket(container_path):
        raise PolicyViolation("docker socket path must never be a mount target")
