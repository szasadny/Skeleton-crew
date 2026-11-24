import csv
import io

from factories import seed_run, seed_task
from web_helpers import Web

from skeleton_crew.domain.task_state import RunTrigger
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.web.pagination import PAGE_SIZE


async def _seed(web: Web, blockers: list[str]) -> tuple[Run, WorkerTask]:
    async with web.sessionmaker() as session:
        run = await seed_run(session)
        task = await seed_task(session, run.id, 12, "Add retries", blockers=blockers, pr_number=42)
        await seed_task(session, run.id, 13, "Clean task")
        await session.commit()
    return run, task


async def test_blockers_page_lists_only_tasks_with_blockers(web: Web) -> None:
    run, _ = await _seed(web, ["No docker", '=HYPERLINK("x")', "<b>x</b>"])
    html = (await web.client.get("/blockers")).text
    assert f'href="/runs/{run.id}"' in html
    assert "No docker" in html
    assert 'href="https://github.com/acme/widgets/pull/42"' in html
    assert "Clean task" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<b>x</b>" not in html


async def test_blockers_page_paginates(web: Web) -> None:
    async with web.sessionmaker() as session:
        run = await seed_run(session, RunTrigger.SCHEDULE, dry_run=True)
        for n in range(PAGE_SIZE + 1):
            await seed_task(session, run.id, n, f"Issue {n}", blockers=["stuck"])
        await session.commit()
    first = (await web.client.get("/blockers")).text
    assert first.count('<a href="/runs/') == PAGE_SIZE
    assert 'href="/blockers?page=2"' in first
    second = (await web.client.get("/blockers?page=2")).text
    assert second.count('<a href="/runs/') == 1
    assert (await web.client.get("/blockers?page=0")).status_code == 422


async def test_blockers_export_is_csv_with_one_row_per_blocker(web: Web) -> None:
    run, task = await _seed(web, ["No docker", "=SUM(A1)", "a, b"])
    response = await web.client.get("/blockers/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="blockers.csv"' in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == [
        "run",
        "task",
        "issue",
        "title",
        "kind",
        "state",
        "pr",
        "updated_at",
        "blocker",
    ]
    assert [row[8] for row in rows[1:]] == ["No docker", "'=SUM(A1)", "a, b"]
    assert all(row[:4] == [str(run.id), str(task.id), "12", "Add retries"] for row in rows[1:])
    assert all(row[6] == "42" for row in rows[1:])
    assert "Clean task" not in response.text


async def test_blockers_export_requires_login(anon_web: Web) -> None:
    response = await anon_web.client.get("/blockers/export")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
