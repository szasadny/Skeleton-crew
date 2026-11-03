import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_settings_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "rule_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_revisions_role_id", "rule_revisions", ["role", "id"])
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "worker_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("issue_title", sa.String(256), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("container_id", sa.String(128), nullable=True),
        sa.Column("ci_rounds", sa.Integer(), nullable=False),
        sa.Column("review_rounds", sa.Integer(), nullable=False),
        sa.Column("policy_rounds", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worker_tasks_run_id", "worker_tasks", ["run_id"])
    op.create_index("ix_worker_tasks_issue_number", "worker_tasks", ["issue_number"])
    op.create_index("ix_worker_tasks_state", "worker_tasks", ["state"])
    op.create_table(
        "refine_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refine_tasks_run_id", "refine_tasks", ["run_id"])
    op.create_index("ix_refine_tasks_issue_number", "refine_tasks", ["issue_number"])
    op.create_index("ix_refine_tasks_state", "refine_tasks", ["state"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_at", "audit_events", ["at"])
    op.create_index("ix_audit_events_kind", "audit_events", ["kind"])
    op.create_index("ix_audit_events_run_id", "audit_events", ["run_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("refine_tasks")
    op.drop_table("worker_tasks")
    op.drop_table("runs")
    op.drop_table("rule_revisions")
    op.drop_table("settings")
