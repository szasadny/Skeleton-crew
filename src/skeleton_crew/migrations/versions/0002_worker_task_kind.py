import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.add_column(sa.Column("kind", sa.String(32), nullable=False, server_default="issue"))
        batch.create_index("ix_worker_tasks_kind", ["kind"])


def downgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.drop_index("ix_worker_tasks_kind")
        batch.drop_column("kind")
