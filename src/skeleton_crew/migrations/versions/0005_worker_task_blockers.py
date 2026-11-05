import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.add_column(sa.Column("blockers", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.drop_column("blockers")
