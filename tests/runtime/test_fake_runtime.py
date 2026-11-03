from pathlib import Path

import pytest
from fake_runtime import FakeRuntime, argv_starts_with, ok

from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.runtime.containers import AgentContainerSpec, ExecResult, Mount


def spec(env: dict[str, str] | None = None) -> AgentContainerSpec:
    return AgentContainerSpec(
        name="a", image="i", network="n", mounts=[Mount(Path("/srv/w"), "/workspace", False)],
        env=env or {}, cpus=1, memory_gb=1, pids=256, labels={},
    )  # fmt: skip


async def test_fake_enforces_the_same_spec_checks() -> None:
    with pytest.raises(PolicyViolation):
        await FakeRuntime().start(spec({"GH_TOKEN": "x"}))


async def test_scripted_exec_first_match_and_once(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.script(argv_starts_with("claude"), ok(b"first"), once=True)
    runtime.script(argv_starts_with("claude"), lambda call: ok(call.cwd.encode()))
    handle = await runtime.start(spec())
    first = await runtime.exec(
        handle, ["claude"], env={}, cwd="/w", user="agent", timeout_s=1,
        stdout_path=tmp_path / "o",
    )  # fmt: skip
    second = await runtime.exec(handle, ["claude"], env={}, cwd="/w", user="agent", timeout_s=1)
    assert (tmp_path / "o").read_bytes() == b"first"
    assert first == ExecResult(0, tmp_path / "o", b"", b"", False)
    assert second.stdout == b"/w"
    assert [c.argv for c in runtime.execs] == [("claude",), ("claude",)]


async def test_unscripted_exec_and_exec_after_remove_fail() -> None:
    runtime = FakeRuntime()
    runtime.script(lambda argv: True, ok())
    handle = await runtime.start(spec())
    assert await runtime.remove_all_managed() == 1
    assert runtime.removed == [handle]
    with pytest.raises(RuntimeError, match="not running"):
        await runtime.exec(handle, ["x"], env={}, cwd="/", user="a", timeout_s=1)
    with pytest.raises(LookupError):
        await FakeRuntime().exec(handle, ["x"], env={}, cwd="/", user="a", timeout_s=1)
