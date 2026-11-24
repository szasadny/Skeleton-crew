from pathlib import Path

import pytest
from git_helpers import GitRepo


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path)
