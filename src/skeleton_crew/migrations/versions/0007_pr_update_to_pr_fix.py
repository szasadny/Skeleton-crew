import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE worker_tasks SET kind = 'pr_fix' WHERE kind = 'pr_update'"))


def downgrade() -> None:
    pass
