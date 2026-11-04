import json
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen literals: a migration must not follow later code changes.
_OLD_KEY = "claude_auth_mode"
_BACKEND = "claude_code"
_OPTION = "auth_mode"


def _rewrite(change: Callable[[dict[str, Any]], None]) -> None:
    bind = op.get_bind()
    for row_id, data in bind.execute(sa.text("SELECT id, data FROM settings")).all():
        settings = json.loads(data)
        change(settings)
        bind.execute(
            sa.text("UPDATE settings SET data = :data WHERE id = :id"),
            {"data": json.dumps(settings), "id": row_id},
        )


def _to_options(settings: dict[str, Any]) -> None:
    mode = settings.pop(_OLD_KEY, None)
    options = settings.setdefault("backend_options", {})
    if mode is not None:
        options.setdefault(_BACKEND, {})[_OPTION] = mode


def _from_options(settings: dict[str, Any]) -> None:
    options = settings.pop("backend_options", {})
    mode = options.get(_BACKEND, {}).get(_OPTION)
    if mode is not None:
        settings[_OLD_KEY] = mode


def upgrade() -> None:
    _rewrite(_to_options)


def downgrade() -> None:
    _rewrite(_from_options)
