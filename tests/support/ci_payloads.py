def check_run(
    name: str, status: str, conclusion: str | None, slug: str = "github-actions"
) -> dict[str, object]:
    return {
        "id": 42,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": slug},
        "details_url": "https://ci/42",
    }


def workflow_run(name: str, status: str, conclusion: str | None) -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": "https://wf"}
