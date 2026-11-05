import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fake_runtime import FakeRuntime, argv_starts_with, ok

from skeleton_crew.runtime.containers import ExecResult
from skeleton_crew.runtime.egress import (
    PLACEHOLDER_DOMAIN,
    RECONFIGURE_ARGV,
    EgressError,
    apply_allowlist,
    render_allowlist,
)

IMAGES = Path(__file__).parents[2] / "src" / "skeleton_crew" / "runtime" / "images"


def test_render_sorts_dedupes_and_normalizes() -> None:
    rendered = render_allowlist(["PyPI.org ", "registry.npmjs.org", "pypi.org"])
    assert rendered == "pypi.org\nregistry.npmjs.org\n"


def test_render_drops_entries_covered_by_a_wildcard() -> None:
    rendered = render_allowlist(["example.com", "api.example.com", ".example.com", "x.org"])
    assert rendered == ".example.com\nx.org\n"


def test_render_keeps_exact_domain_without_wildcard_exact() -> None:
    assert render_allowlist(["api.example.com"]) == "api.example.com\n"
    assert render_allowlist(["notexample.com", ".example.com"]) == (
        ".example.com\nnotexample.com\n"
    )


def test_render_empty_uses_placeholder_matching_nothing() -> None:
    assert render_allowlist([]) == f"{PLACEHOLDER_DOMAIN}\n"


@pytest.mark.parametrize(
    "domain",
    ["localhost", "*.example.com", "example.com/path", "10.0.0.1", "a b.com", "evil.com\nall"],
)
def test_render_rejects_invalid_domains(domain: str) -> None:
    with pytest.raises(ValueError, match="invalid egress domain"):
        render_allowlist(["pypi.org", domain])


async def test_apply_writes_file_and_reconfigures_proxy(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.script(argv_starts_with(*RECONFIGURE_ARGV), ok())
    target = tmp_path / "egress" / "allowlist.txt"

    await apply_allowlist(runtime, ["pypi.org"], target, "skeleton-crew-egress-proxy")

    assert target.read_text() == "pypi.org\n"
    assert target.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in target.parent.iterdir()] == ["allowlist.txt"]
    (call,) = runtime.execs
    assert call.handle.id == "skeleton-crew-egress-proxy"
    assert call.argv == RECONFIGURE_ARGV


async def test_apply_replaces_existing_file(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.script(argv_starts_with(*RECONFIGURE_ARGV), ok())
    target = tmp_path / "allowlist.txt"
    target.write_text("old.example.com\n")
    await apply_allowlist(runtime, ["new.example.com"], target, "proxy")
    assert target.read_text() == "new.example.com\n"


async def test_apply_invalid_domain_leaves_file_untouched(tmp_path: Path) -> None:
    target = tmp_path / "allowlist.txt"
    target.write_text("pypi.org\n")
    with pytest.raises(ValueError, match="invalid egress domain"):
        await apply_allowlist(FakeRuntime(), ["bad domain"], target, "proxy")
    assert target.read_text() == "pypi.org\n"


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(1, None, b"", b"FATAL: bad config", False),
        ExecResult(124, None, b"", b"", True),
    ],
)
async def test_apply_fails_when_reconfigure_fails(tmp_path: Path, result: ExecResult) -> None:
    runtime = FakeRuntime()
    runtime.script(argv_starts_with(*RECONFIGURE_ARGV), result)
    with pytest.raises(EgressError, match="reconfigure failed"):
        await apply_allowlist(runtime, ["pypi.org"], tmp_path / "a.txt", "proxy")


def test_squid_config_is_deny_by_default() -> None:
    lines = [
        line.strip()
        for line in (IMAGES / "egress-proxy" / "squid.conf").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    access = [line for line in lines if line.startswith("http_access")]
    assert access[-1] == "http_access deny all"
    assert sum(line.startswith("http_access allow") for line in access) == 2
    assert all("allowlist" in line for line in access if line.startswith("http_access allow"))
    assert 'acl allowlist dstdomain -n "/etc/squid/allowlist/allowlist.txt"' in lines
    assert "acl SSL_ports port 443" in lines
    assert "acl HTTP_ports port 80" in lines
    for directive in ("cache deny all", "via off", "forwarded_for delete"):
        assert directive in lines


def test_proxy_image_ships_placeholder_allowlist() -> None:
    dockerfile = (IMAGES / "egress-proxy" / "Dockerfile").read_text()
    assert f'echo "{PLACEHOLDER_DOMAIN}" > /etc/squid/allowlist/allowlist.txt' in dockerfile


def test_agent_image_trusts_only_the_workspace() -> None:
    dockerfile = (IMAGES / "agent" / "Dockerfile").read_text()
    assert "safe.directory /workspace" in dockerfile
    assert "safe.directory '*'" not in dockerfile
    assert "safe.directory *" not in dockerfile
    assert "ARG CLAUDE_CODE_VERSION" in dockerfile


def test_agent_image_pins_mise_by_version_and_checksum() -> None:
    dockerfile = (IMAGES / "agent" / "Dockerfile").read_text()
    assert re.search(r"^ARG MISE_VERSION=\d+\.\d+\.\d+$", dockerfile, re.MULTILINE)
    assert re.search(r"^ARG MISE_SHA256=[0-9a-f]{64}$", dockerfile, re.MULTILINE)
    assert "releases/download/v${MISE_VERSION}/" in dockerfile
    assert 'echo "${MISE_SHA256}  /tmp/mise" | sha256sum -c -' in dockerfile
    assert "mise/releases/latest" not in dockerfile


@pytest.mark.parametrize("script", ["agent/entrypoint.sh", "egress-proxy/entrypoint.sh"])
def test_entrypoints_are_valid_bash(script: str) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not installed")
    subprocess.run([bash, "-n", str(IMAGES / script)], check=True)  # noqa: S603
