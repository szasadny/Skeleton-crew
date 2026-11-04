from alembic import op

revision = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.alter_column("escalation_reason", new_column_name="note")


def downgrade() -> None:
    with op.batch_alter_table("worker_tasks") as batch:
        batch.alter_column("note", new_column_name="escalation_reason")
