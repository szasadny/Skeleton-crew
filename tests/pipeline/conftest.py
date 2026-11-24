from pathlib import Path

import pytest
from pipeline_helpers import Harness, selection_json
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.domain.settings import Role


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    return Harness(tmp_path, sessionmaker)


@pytest.fixture
def ready_issue(harness: Harness) -> None:
    harness.add_ready_issue()


@pytest.fixture
def selected_issue(harness: Harness, ready_issue: None) -> None:
    harness.backend.script(Role.SELECTOR, selection_json([1]))
