"""Add the current operational occurrence root and queue projection.

Revision ID: platform_0008
Revises: platform_0007
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0008"
down_revision: str | None = "platform_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operational_occurrence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("occurrence_no", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("signal_state", sa.String(length=16), nullable=False),
        sa.Column("signal_severity", sa.String(length=16), nullable=False),
        sa.Column("response_state", sa.String(length=24), nullable=False),
        sa.Column("response_priority", sa.String(length=2), nullable=False),
        sa.Column("resolution_code", sa.String(length=24), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("assignment_origin", sa.String(length=16), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=True),
        sa.Column("source_started_at", sa.DateTime(), nullable=True),
        sa.Column("ack_sla_seconds", sa.Integer(), nullable=False),
        sa.Column("ack_sla_due_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("first_investigating_at", sa.DateTime(), nullable=True),
        sa.Column("mitigated_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("latest_activity_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint(
            "incident_id", "occurrence_no", name="uq_operational_occurrence_number"
        ),
        sa.CheckConstraint(
            "signal_state IN ('FIRING','RECOVERED','UNKNOWN','STALE')",
            name="ck_operational_occurrence_signal",
        ),
        sa.CheckConstraint(
            "response_state IN ('UNACKNOWLEDGED','ACKNOWLEDGED','INVESTIGATING','MITIGATING','MONITORING','RESOLVED')",
            name="ck_operational_occurrence_response",
        ),
        sa.CheckConstraint(
            "response_priority IN ('P1','P2','P3','P4')",
            name="ck_operational_occurrence_priority",
        ),
        sa.CheckConstraint(
            "assignment_origin IN ('UNMAPPED','MAPPING','MANUAL')",
            name="ck_operational_occurrence_assignment",
        ),
        sa.CheckConstraint("ack_sla_seconds > 0", name="ck_operational_occurrence_sla"),
    )
    op.create_index(
        "ix_operational_occurrence_queue",
        "operational_occurrence",
        ["response_state", "ack_sla_due_at", "response_priority", "latest_activity_at", "id"],
    )
    op.create_index(
        "ix_operational_occurrence_source",
        "operational_occurrence",
        ["source_id", "response_state", "latest_activity_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operational_occurrence_source", table_name="operational_occurrence"
    )
    op.drop_index(
        "ix_operational_occurrence_queue", table_name="operational_occurrence"
    )
    op.drop_table("operational_occurrence")
