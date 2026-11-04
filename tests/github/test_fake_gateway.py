from pathlib import Path

import pytest
from fake_gateway import BOT_LOGIN, FakeGitHubGateway, FakePull
from github_helpers import SHA, commit_on_top, git, make_bare_remote

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import LabelChange, escalation_change
from skeleton_crew.domain.settings import LabelNames
from skeleton_crew.github.gateway import CiState, Gateway


def test_fake_satisfies_gateway_protocol() -> None:
    gateway: Gateway = FakeGitHubGateway()
    assert gateway.base_branch == "main"


async def test_fake_runs_label_policy_and_audits_block() -> None:
    gw = FakeGitHubGateway()
    gw.add_issue(1, "a", labels=["skeleton HITL"])

    with pytest.raises(PolicyViolation):
        await gw.change_labels(LabelChange(1, frozenset({"skeleton"}), frozenset()))

    assert gw.writes == []
    assert [(a.kind, a.outcome) for a in gw.audit_events] == [
        (AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED)
    ]


async def test_fake_applies_allowed_label_change() -> None:
    gw = FakeGitHubGateway()
    gw.add_issue(1, "a", labels=["Skeleton"])

    await gw.change_labels(escalation_change(1, LabelNames()))

    assert gw.issues[1].labels == {"skeleton HITL"}
    assert gw.issues[1].label_events[-1].actor == BOT_LOGIN


async def test_fake_dry_run_records_no_writes() -> None:
    gw = FakeGitHubGateway(dry_run=True)
    gw.add_issue(1, "a", labels=["skeleton"])

    await gw.change_labels(escalation_change(1, LabelNames()))
    await gw.comment(1, "x")
    assert await gw.create_hitl_issue("t", "b") is None
    assert await gw.open_pr(branch="skeleton/1-a", title="t", body="b") is None
    await gw.push_branch(Path("/nonexistent"), "skeleton/1-a", SHA)

    assert gw.writes == []
    assert gw.issues[1].labels == {"skeleton"}
    assert {a.outcome for a in gw.audit_events} == {AuditOutcome.DRY_RUN}


async def test_fake_blocks_push_to_main() -> None:
    gw = FakeGitHubGateway()

    with pytest.raises(PolicyViolation):
        await gw.push_branch(Path("/nonexistent"), "main", SHA)


async def test_fake_scripted_ci_advances_and_sticks_on_last() -> None:
    gw = FakeGitHubGateway()
    gw.script_ci(SHA, CiState.PENDING, CiState.SUCCESS)

    states = [(await gw.ci_status(SHA)).state for _ in range(3)]

    assert states == [CiState.PENDING, CiState.SUCCESS, CiState.SUCCESS]
    assert (await gw.ci_status("b" * 40)).state == CiState.NONE


async def test_fake_open_skeleton_prs_ignore_forks_and_closed_pulls() -> None:
    gw = FakeGitHubGateway()
    gw.pulls.append(FakePull(5, "skeleton/3-x", SHA, same_repo=False))
    gw.pulls.append(FakePull(7, "skeleton/3-x", SHA, state="closed"))
    assert await gw.open_skeleton_prs() == {}
    gw.pulls.append(FakePull(6, "skeleton/3-x", SHA))
    prs = await gw.open_skeleton_prs()
    assert [p.number for p in prs[3]] == [6]


async def test_fake_closed_unmerged_pr_ignores_merged_and_open_pulls() -> None:
    gw = FakeGitHubGateway()
    gw.pulls.append(FakePull(5, "skeleton/3-x", SHA))
    gw.pulls.append(FakePull(6, "skeleton/3-x", SHA, state="closed", merged=True))
    assert await gw.closed_unmerged_pr_for("skeleton/3-x") is None
    gw.pulls.append(FakePull(7, "skeleton/3-x", SHA, state="closed"))
    pr = await gw.closed_unmerged_pr_for("skeleton/3-x")
    assert pr is not None
    assert pr.number == 7
    with pytest.raises(PolicyViolation):
        await gw.closed_unmerged_pr_for("main")


async def test_fake_mergeable_advances_and_sticks_on_last() -> None:
    gw = FakeGitHubGateway()
    gw.mergeable[4] = [None, False]

    assert [await gw.pr_mergeable(4) for _ in range(3)] == [None, False, False]
    assert await gw.pr_mergeable(9) is None


async def test_fake_hitl_issue_gets_only_hitl_label() -> None:
    gw = FakeGitHubGateway(max_comment_chars=200)
    gw.add_issue(4, "a")

    number = await gw.create_hitl_issue("found", "z" * 500)

    assert number == 5
    assert gw.issues[5].labels == {"skeleton HITL"}
    assert len(gw.issues[5].body) == 200


async def test_fake_clones_source_and_pushes_to_local_remote(tmp_path: Path) -> None:
    bare, sha = make_bare_remote(tmp_path)
    gw = FakeGitHubGateway(source_repo=bare, push_remote=bare)
    clone = tmp_path / "clone"

    assert await gw.clone(clone) == sha
    new = commit_on_top(clone, "f.txt")
    await gw.push_branch(clone, "skeleton/1-a", new)
    pr = await gw.open_pr(branch="skeleton/1-a", title="t", body="Closes #1")

    assert git(bare, "rev-parse", "refs/heads/skeleton/1-a") == new
    assert pr is not None
    assert pr.head_sha == new
