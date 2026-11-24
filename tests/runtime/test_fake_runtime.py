from pathlib import Path

import pytest
from fake_runtime import FakeRuntime, argv_starts_with, ok
from runtime_helpers import container_spec

from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.runtime.containers import MANAGED_LABEL, MANAGED_VALUE, ExecRequest, ExecResult


async def test_fake_enforces_the_same_spec_checks(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation):
        await FakeRuntime().start(container_spec(tmp_path, env={"GH_TOKEN": "x"}))


async def test_scripted_exec_first_match_and_once(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.script(argv_starts_with("claude"), ok(b"first"), once=True)
    runtime.script(argv_starts_with("claude"), lambda call: ok(call.cwd.encode()))
    handle = await runtime.start(container_spec(tmp_path))
    out = tmp_path / "o"
    first = await runtime.exec(handle, ExecRequest(["claude"], "agent", "/w", 1, stdout_path=out))
    second = await runtime.exec(handle, ExecRequest(["claude"], "agent", "/w", 1))
    assert out.read_bytes() == b"first"
    assert first == ExecResult(0, b"", b"", False)
    assert second.stdout == b"/w"
    assert [c.argv for c in runtime.execs] == [("claude",), ("claude",)]


async def test_unscripted_exec_and_exec_after_remove_fail(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.script(lambda argv: True, ok())
    handle = await runtime.start(container_spec(tmp_path))
    assert await runtime.remove_labeled(MANAGED_LABEL, MANAGED_VALUE) == 1
    assert runtime.removed == [handle]
    with pytest.raises(RuntimeError, match="not running"):
        await runtime.exec(handle, ExecRequest.as_root(["x"], timeout_s=1))
    with pytest.raises(LookupError):
        await FakeRuntime().exec(handle, ExecRequest.as_root(["x"], timeout_s=1))
