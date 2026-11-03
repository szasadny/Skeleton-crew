from alembic import context
from sqlalchemy import Connection

import skeleton_crew.models  # noqa: F401
from skeleton_crew.core.db import Base


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


# run_migrations() hands over a sync connection obtained via AsyncConnection.run_sync.
_connection = context.config.attributes.get("connection")
if not isinstance(_connection, Connection):
    raise RuntimeError("migrations run only through skeleton_crew.core.db.run_migrations")
_run(_connection)
