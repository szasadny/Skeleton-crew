import pytest

from skeleton_crew.domain.container_policy import check_env, check_mount
from skeleton_crew.domain.errors import PolicyViolation


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GH_TOKEN", "x"),
        ("GITHUB_TOKEN", "x"),
        ("github_token", "x"),
        ("MY_GITHUB_APP_TOKEN", "x"),
        ("GH_ENTERPRISE_TOKEN", "x"),
        ("GIT_CONFIG_VALUE_0", "Authorization: Basic abc"),
        ("git_config_count", "1"),
        ("INNOCENT", "ghs_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("INNOCENT", "github_pat_abc"),
        ("REMOTE", "https://X-Access-Token:abc@github.com/o/r"),
    ],
)
def test_github_credentials_are_refused(key: str, value: str) -> None:
    with pytest.raises(PolicyViolation, match=f"GitHub credential in container env: {key}"):
        check_env({"HTTPS_PROXY": "http://egress-proxy:3128", key: value})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x"),
        ("ANTHROPIC_API_KEY", "sk-ant-x"),
        ("TOKENIZER", "ghost"),
        ("GITHUB_URL", "https://github.com"),
    ],
)
def test_model_credentials_and_lookalikes_pass(key: str, value: str) -> None:
    check_env({key: value})


@pytest.mark.parametrize(
    ("host", "resolved", "target"),
    [
        ("/var/run/docker.sock", "/var/run/docker.sock", "/data"),
        ("/run/docker.sock", "/run/docker.sock", "/data"),
        ("/srv/elsewhere/docker.sock", "/srv/elsewhere/docker.sock", "/sock"),
        ("/srv/innocent", "/var/run/docker.sock", "/data"),
    ],
)
def test_docker_socket_is_never_mounted(host: str, resolved: str, target: str) -> None:
    with pytest.raises(PolicyViolation, match="docker socket must never be mounted"):
        check_mount(host, resolved, target)


@pytest.mark.parametrize("target", ["/var/run/docker.sock", "/run/docker.sock", "/x/docker.sock"])
def test_docker_socket_path_is_never_a_mount_target(target: str) -> None:
    with pytest.raises(PolicyViolation, match="mount target"):
        check_mount("/srv/data", "/srv/data", target)


def test_ordinary_mount_passes() -> None:
    check_mount("/srv/agent", "/srv/agent", "/workspace")
