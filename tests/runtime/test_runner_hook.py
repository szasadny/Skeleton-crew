import shutil
import subprocess
from pathlib import Path

import pytest

from skeleton_crew.domain.branch_policy import BRANCH_PREFIX

HOOK = Path(__file__).parents[2] / "src/skeleton_crew/runtime/images/runner/job-started.sh"
DOCKERFILE = HOOK.parent / "Dockerfile"


def run_hook(prefix: str | None = BRANCH_PREFIX, **env: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not installed")
    if prefix is not None:
        env["SC_BRANCH_PREFIX"] = prefix
    return subprocess.run(  # noqa: S603
        [bash, str(HOOK)], env={"PATH": "/usr/bin:/bin", **env}, capture_output=True, text=True
    )


def test_skeleton_pull_request_passes() -> None:
    result = run_hook(GITHUB_HEAD_REF="skeleton/12-fix", GITHUB_REF_NAME="34/merge")
    assert (result.returncode, result.stdout) == (0, "")


def test_skeleton_branch_push_passes() -> None:
    result = run_hook(GITHUB_HEAD_REF="", GITHUB_REF_NAME="skeleton/7-x")
    assert result.returncode == 0


def test_push_to_main_is_refused_with_the_fix() -> None:
    result = run_hook(GITHUB_HEAD_REF="", GITHUB_REF_NAME="main")
    assert result.returncode == 1
    assert result.stdout.startswith("::error title=Not a Skeleton Crew job::")
    assert "'main'" in result.stdout
    assert "startsWith(github.head_ref, 'skeleton/')" in result.stdout
    assert "vars.CI_RUNNER" in result.stdout


def test_human_pull_request_is_refused() -> None:
    result = run_hook(GITHUB_HEAD_REF="feature/x", GITHUB_REF_NAME="skeleton/1")
    assert result.returncode == 1
    assert "'feature/x'" in result.stdout


@pytest.mark.parametrize("branch", ["skeleton", "xskeleton/1", "Skeleton/1", " skeleton/1"])
def test_lookalike_branches_are_refused(branch: str) -> None:
    result = run_hook(GITHUB_HEAD_REF=branch)
    assert result.returncode == 1


def test_glob_characters_in_the_prefix_are_literal() -> None:
    result = run_hook("skeleton/*", GITHUB_HEAD_REF="skeleton/1")
    assert result.returncode == 1


def test_no_branch_is_refused() -> None:
    assert run_hook().returncode == 1


@pytest.mark.parametrize("prefix", [None, ""])
def test_missing_prefix_fails_closed(prefix: str | None) -> None:
    assert run_hook(prefix, GITHUB_HEAD_REF="skeleton/1").returncode == 1


def test_runner_image_installs_the_hook() -> None:
    dockerfile = DOCKERFILE.read_text()
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/job-started.sh" in dockerfile
    assert "job-started.sh /usr/local/bin/" in dockerfile
