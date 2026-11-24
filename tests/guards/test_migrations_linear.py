import re
from importlib.resources import files
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_FILE_NAME = re.compile(r"(\d{4})_[a-z0-9_]+\.py")


def _scripts() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(files("skeleton_crew") / "migrations"))
    return ScriptDirectory.from_config(config)


def test_migrations_form_one_numbered_chain() -> None:
    scripts = _scripts()
    assert len(scripts.get_heads()) == 1
    chain = list(reversed(list(scripts.walk_revisions())))
    assert [script.revision for script in chain] == [f"{n:04d}" for n in range(1, len(chain) + 1)]
    assert [script.down_revision for script in chain] == [
        None,
        *(script.revision for script in chain[:-1]),
    ]


def test_migration_files_are_named_after_their_revision() -> None:
    for script in _scripts().walk_revisions():
        match = _FILE_NAME.fullmatch(Path(script.path).name)
        assert match is not None, script.path
        assert match.group(1) == script.revision
