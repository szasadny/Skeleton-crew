import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.add_column(
            sa.Column("check_rounds", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.drop_column("check_rounds")
