"""Add the dedicated append-only gate for bounded operations retention.

Revision ID: platform_0029
Revises: platform_0028
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0029"
down_revision: str | None = "platform_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operations_retention_gate",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id=1", name="ck_operations_retention_gate_id"),
    )
    op.execute("INSERT INTO operations_retention_gate (id,enabled) VALUES (1,0)")
    op.execute("DROP TRIGGER IF EXISTS incident_timeline_no_delete")
    op.execute(
        "CREATE TRIGGER incident_timeline_no_delete "
        "BEFORE DELETE ON incident_timeline_entry "
        "WHEN (SELECT enabled FROM operations_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'INCIDENT_TIMELINE_APPEND_ONLY'); END"
    )
    op.create_index(
        "ix_platform_job_retention",
        "platform_job",
        ["state", "finished_at", "id"],
    )
    op.create_index(
        "ix_platform_event_retention",
        "platform_event",
        ["subject_type", "created_at", "sequence"],
    )
    op.create_index(
        "ix_operational_occurrence_retention",
        "operational_occurrence",
        ["response_state", "resolved_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operational_occurrence_retention",
        table_name="operational_occurrence",
    )
    op.drop_index("ix_platform_event_retention", table_name="platform_event")
    op.drop_index("ix_platform_job_retention", table_name="platform_job")
    op.execute("DROP TRIGGER IF EXISTS incident_timeline_no_delete")
    op.execute(
        "CREATE TRIGGER incident_timeline_no_delete "
        "BEFORE DELETE ON incident_timeline_entry "
        "BEGIN SELECT RAISE(ABORT, 'INCIDENT_TIMELINE_APPEND_ONLY'); END"
    )
    op.drop_table("operations_retention_gate")
