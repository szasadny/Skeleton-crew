import json
import secrets
import shutil
import string
from collections.abc import Sequence

import pytest

from skeleton_crew.runtime import secret_scan
from skeleton_crew.runtime.secret_scan import GITLEAKS_ARGV, SecretScanError, scan_patch

PATCH = (
    b"diff --git a/a.txt b/a.txt\n"
    b"--- a/a.txt\n"
    b"+++ b/a.txt\n"
    b"@@ -1 +1 @@\n"
    b"-x\n"
    b"+y\n"
    b"diff --git a/conf/settings.py b/conf/settings.py\n"
    b"--- a/conf/settings.py\n"
    b"+++ b/conf/settings.py\n"
    b"@@ -0,0 +1 @@\n"
    b"+TOKEN = 'redacted'\n"
)


def fixed(exit_code: int, stdout: bytes, stderr: bytes = b"") -> secret_scan.Runner:
    async def runner(argv: Sequence[str], stdin: bytes) -> tuple[int, bytes, bytes]:
        assert tuple(argv) == GITLEAKS_ARGV
        assert stdin == PATCH
        return exit_code, stdout, stderr

    return runner


async def test_findings_describe_rule_and_file_but_never_the_secret() -> None:
    report = [
        {"RuleID": "github-pat", "File": "", "StartLine": 11, "Secret": "REDACTED", "Match": "m"},
        {"RuleID": "generic-api-key", "File": "x.env", "StartLine": 1, "Secret": "s"},
        {"RuleID": "github-pat", "StartLine": 11},
    ]
    findings = await scan_patch(PATCH, runner=fixed(0, json.dumps(report).encode()))
    assert findings == ["generic-api-key in x.env", "github-pat in conf/settings.py"]
    assert not any("REDACTED" in f or " m" in f for f in findings)


async def test_no_findings() -> None:
    assert await scan_patch(PATCH, runner=fixed(0, b"[]\n")) == []


async def test_missing_binary_fails_closed() -> None:
    async def missing(argv: Sequence[str], stdin: bytes) -> tuple[int, bytes, bytes]:
        raise FileNotFoundError(argv[0])

    with pytest.raises(SecretScanError, match="not installed"):
        await scan_patch(PATCH, runner=missing)


async def test_default_runner_without_binary_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_scan, "GITLEAKS_ARGV", ("sc-no-such-gitleaks-binary", "stdin"))
    with pytest.raises(SecretScanError, match="not installed"):
        await scan_patch(PATCH)


@pytest.mark.parametrize(
    ("exit_code", "stdout"),
    [(1, b"[]"), (0, b""), (0, b"not json"), (0, b"{}"), (0, b"[1]")],
)
async def test_unusable_output_fails_closed(exit_code: int, stdout: bytes) -> None:
    with pytest.raises(SecretScanError):
        await scan_patch(PATCH, runner=fixed(exit_code, stdout))


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks binary not installed")
async def test_real_gitleaks_flags_a_github_token() -> None:
    fake_token = "ghp_" + "".join(secrets.choice(string.ascii_letters) for _ in range(36))
    patch = (
        b"diff --git a/cfg.py b/cfg.py\n--- /dev/null\n+++ b/cfg.py\n@@ -0,0 +1 @@\n"
        + f"+TOKEN = '{fake_token}'\n".encode()
    )
    findings = await scan_patch(patch)
    assert findings
    assert not any(fake_token in f for f in findings)
    assert await scan_patch(b"diff --git a/x b/x\n+hello\n") == []
