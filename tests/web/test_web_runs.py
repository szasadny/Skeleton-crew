import os
from pathlib import Path

import pytest
from factories import seed_refine, seed_run, seed_task
from web_helpers import TOKEN_SECRET, Web

from skeleton_crew.domain.task_keys import refine_key, selector_key, worker_key
from skeleton_crew.domain.task_state import RefineState, RunState, RunTrigger, TaskState
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.store import runs_store
from skeleton_crew.web.pagination import PAGE_SIZE
from skeleton_crew.web.transcripts import MAX_TRANSCRIPT_BYTES


async def _run_with_task(web: Web) -> tuple[Run, WorkerTask, RefineTask]:
    async with web.sessionmaker() as session:
        run = await seed_run(session)
        task = await seed_task(
            session,
            run.id,
            12,
            "Add retries",
            state=TaskState.ESCALATED,
            pr_number=42,
            ci_rounds=3,
            cost_usd=1.234,
            note="max CI rounds reached",
        )
        refine = await seed_refine(session, run.id, 99, state=RefineState.DONE)
        await runs_store.finish_run(session, run.id, RunState.COMPLETED)
        await session.commit()
    return run, task, refine


def _transcripts_dir(web: Web, key: str) -> Path:
    directory = web.config.tasks_dir / key / "transcripts"
    directory.mkdir(parents=True)
    return directory


def _worker_url(run: Run, task: WorkerTask, name: str = "") -> str:
    url = f"/runs/{run.id}/transcripts/{worker_key(task.id)}"
    return f"{url}/{name}" if name else url


async def test_runs_list_paginates(web: Web) -> None:
    async with web.sessionmaker() as session:
        for _ in range(PAGE_SIZE + 5):
            await seed_run(session, RunTrigger.SCHEDULE, dry_run=True)
        await session.commit()
    first = (await web.client.get("/runs")).text
    assert 'href="/runs?page=2"' in first
    assert first.count('<a href="/runs/') == PAGE_SIZE
    second = (await web.client.get("/runs?page=2")).text
    assert second.count('<a href="/runs/') == 5
    assert 'href="/runs?page=3"' not in second
    assert (await web.client.get("/runs?page=0")).status_code == 422


async def test_run_detail_shows_tasks(web: Web) -> None:
    run, task, _ = await _run_with_task(web)
    html = (await web.client.get(f"/runs/{run.id}")).text
    assert 'href="https://github.com/acme/widgets/pull/42"' in html
    assert "max CI rounds reached" in html
    assert "3 / 0 / 0" in html
    assert "$1.23" in html
    assert _worker_url(run, task) in html
    assert "#99" in html


async def test_unknown_run_is_an_html_404(web: Web) -> None:
    response = await web.client.get("/runs/9999")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


async def test_run_detail_links_every_transcript_key(web: Web) -> None:
    run, task, refine = await _run_with_task(web)
    html = (await web.client.get(f"/runs/{run.id}")).text
    for key in (worker_key(task.id), refine_key(refine.id), selector_key(run.id)):
        assert f'href="/runs/{run.id}/transcripts/{key}"' in html
        assert (await web.client.get(f"/runs/{run.id}/transcripts/{key}")).status_code == 200


async def test_transcript_list_and_view_are_redacted(web: Web) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    line = f'{{"type":"assistant","text":"token {TOKEN_SECRET} <b>x</b>"}}\n'
    (directory / "worker-1.jsonl").write_text(line)
    (directory / "notes.txt").write_text("hidden")
    listing = (await web.client.get(_worker_url(run, task))).text
    assert "worker-1.jsonl" in listing
    assert "notes.txt" not in listing
    view = await web.client.get(_worker_url(run, task, "worker-1.jsonl"))
    assert view.status_code == 200
    assert TOKEN_SECRET not in view.text
    assert "token ***" in view.text
    assert "&lt;b&gt;x&lt;/b&gt;" in view.text
    assert "Showing the last" not in view.text


@pytest.mark.parametrize("name", ["worker-1.jsonl", "check-1.log"])
async def test_large_transcript_shows_only_the_tail(web: Web, name: str) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    body = "".join(f"line {i:07d}\n" for i in range(MAX_TRANSCRIPT_BYTES // 10))
    (directory / name).write_text(f"HEAD {TOKEN_SECRET}\n{body}LAST LINE\n")
    view = await web.client.get(_worker_url(run, task, name))
    assert "Showing the last 500 KB only." in view.text
    assert "LAST LINE" in view.text
    assert "HEAD" not in view.text


@pytest.mark.parametrize(
    "name",
    [
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "%2E%2E",
        "worker-1.txt",
        "Worker-1.jsonl",
        "worker-1.jsonl%00",
        "worker-.jsonl",
        "missing-1.jsonl",
    ],
)
async def test_transcript_name_traversal_is_rejected(web: Web, name: str) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    (directory / "worker-1.jsonl").write_text("ok\n")
    assert (await web.client.get(_worker_url(run, task, name))).status_code == 404


@pytest.mark.parametrize(
    "key", ["worker-9999", "%2E%2E", "%2E%2E%2F%2E%2E", "worker-1%2F..", "tasks"]
)
async def test_transcript_key_outside_the_run_is_rejected(web: Web, key: str) -> None:
    run, _, _ = await _run_with_task(web)
    assert (await web.client.get(f"/runs/{run.id}/transcripts/{key}")).status_code == 404


async def test_symlinked_transcript_is_rejected(web: Web, tmp_path: Path) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    outside = tmp_path / "secret.txt"
    outside.write_text("do not show")
    os.symlink(outside, directory / "worker-2.jsonl")
    listing = (await web.client.get(_worker_url(run, task))).text
    assert "worker-2.jsonl" not in listing
    assert (await web.client.get(_worker_url(run, task, "worker-2.jsonl"))).status_code == 404


async def test_transcript_list_sorts_numbers_numerically(web: Web) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    names = ["worker-10.jsonl", "check-1.log", "worker-2.jsonl", "reviewer-1.jsonl", "check-12.log"]
    for name in names:
        (directory / name).write_text("{}\n")
    listing = (await web.client.get(_worker_url(run, task))).text
    order = sorted(names, key=listing.index)
    assert order == [
        "check-1.log",
        "check-12.log",
        "reviewer-1.jsonl",
        "worker-2.jsonl",
        "worker-10.jsonl",
    ]


async def test_selector_transcripts_of_the_run(web: Web) -> None:
    run, _, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, selector_key(run.id))
    (directory / "selector-1.jsonl").write_text("{}\n")
    view = await web.client.get(
        f"/runs/{run.id}/transcripts/{selector_key(run.id)}/selector-1.jsonl"
    )
    assert view.status_code == 200


async def test_repo_command_logs_are_listed_and_redacted(web: Web) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    (directory / "setup-1.log").write_text(f"installing with {TOKEN_SECRET}\n")
    (directory / "check-2.log").write_text("tests <b>ok</b>\n")
    (directory / "other-1.log").write_text("hidden")
    listing = (await web.client.get(_worker_url(run, task))).text
    assert "setup-1.log" in listing
    assert "check-2.log" in listing
    assert "other-1.log" not in listing
    setup = await web.client.get(_worker_url(run, task, "setup-1.log"))
    assert setup.status_code == 200
    assert TOKEN_SECRET not in setup.text
    assert "installing with ***" in setup.text
    check = await web.client.get(_worker_url(run, task, "check-2.log"))
    assert "tests &lt;b&gt;ok&lt;/b&gt;" in check.text


@pytest.mark.parametrize(
    "name",
    [
        "..%2Fsetup-1.log",
        "..%2F..%2Fsetup-1.log",
        "setup-1.log%2F..",
        "setup-1.txt",
        "setup-1.log.bak",
        "setup-1.logx",
        "setup-1.jsonl.log",
        "Setup-1.log",
        "setup-.log",
        "setup-%D9%A1.log",
        "other-1.log",
        "setup-1.sh",
    ],
)
async def test_repo_command_log_bad_names_are_rejected(web: Web, name: str) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    hidden = ("setup-1.txt", "setup-1.log.bak", "setup-1.sh", "other-1.log")
    for present in ("setup-1.log", *hidden):
        (directory / present).write_text("x\n")
    assert (await web.client.get(_worker_url(run, task, name))).status_code == 404
    listing = (await web.client.get(_worker_url(run, task))).text
    assert not [entry for entry in hidden if entry in listing]


async def test_symlinked_repo_command_log_is_rejected(web: Web, tmp_path: Path) -> None:
    run, task, _ = await _run_with_task(web)
    directory = _transcripts_dir(web, worker_key(task.id))
    outside = tmp_path / "secret.txt"
    outside.write_text("do not show")
    os.symlink(outside, directory / "check-1.log")
    listing = (await web.client.get(_worker_url(run, task))).text
    assert "check-1.log" not in listing
    assert (await web.client.get(_worker_url(run, task, "check-1.log"))).status_code == 404


async def test_run_detail_lists_blockers_escaped(web: Web) -> None:
    run, task, _ = await _run_with_task(web)
    async with web.sessionmaker() as session:
        await runs_store.update_worker_task(
            session,
            task.id,
            blockers=["worker: Python 3.13 is missing", "reviewer: <script>alert(1)</script>"],
        )
        await session.commit()
    html = (await web.client.get(f"/runs/{run.id}")).text
    assert 'class="blockers-row"' in html
    assert "<li>worker: Python 3.13 is missing</li>" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


async def test_run_detail_without_blockers_has_no_blockers_row(web: Web) -> None:
    run, _, _ = await _run_with_task(web)
    assert 'class="blockers-row"' not in (await web.client.get(f"/runs/{run.id}")).text


async def test_runs_list_counts_tasks_with_blockers(web: Web) -> None:
    async with web.sessionmaker() as session:
        blocked = await seed_run(session)
        for number, blockers in ((1, ["worker: a"]), (2, None), (3, [])):
            await seed_task(session, blocked.id, number, blockers=blockers)
        clean = await seed_run(session)
        await seed_task(session, clean.id, 4)
        await session.commit()
    html = (await web.client.get("/runs")).text
    assert html.count("with blockers") == 1
    assert "1 task with blockers" in html
