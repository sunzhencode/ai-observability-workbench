"""Add current handling, immutable audit and append-only occurrence history.

Revision ID: platform_0006
Revises: platform_0005
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0006"
down_revision: str | None = "platform_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "incident",
        sa.Column("handling_state", sa.String(length=24), nullable=False, server_default="NEW"),
    )
    op.add_column(
        "incident",
        sa.Column("handling_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "incident",
        sa.Column("change_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "incident",
        sa.Column("change_origin", sa.String(length=32), nullable=False, server_default="SOURCE_ACTIVATION"),
    )
    op.create_index("ix_incident_handling_state", "incident", ["handling_state", "updated_at"])
    op.create_table(
        "incident_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=False),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "from_state IN ('NEW','IN_PROGRESS','CLOSED','FALSE_POSITIVE')",
            name="ck_incident_audit_from_state",
        ),
        sa.CheckConstraint(
            "to_state IN ('NEW','IN_PROGRESS','CLOSED','FALSE_POSITIVE')",
            name="ck_incident_audit_to_state",
        ),
    )
    op.create_index(
        "ix_incident_audit_incident_created",
        "incident_audit",
        ["incident_id", "created_at", "id"],
    )
    op.create_table(
        "incident_occurrence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("occurrence_no", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_name", sa.String(length=160), nullable=False),
        sa.Column("group_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("aggregation_rule_id", sa.Integer(), nullable=True),
        sa.Column("aggregation_rule_name", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("recovered_at", sa.DateTime(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("member_max_severity", sa.String(length=16), nullable=False),
        sa.Column("handling_conclusion", sa.String(length=24), nullable=False),
        sa.UniqueConstraint("incident_id", "occurrence_no", name="uq_incident_occurrence"),
        sa.CheckConstraint("member_count > 0", name="ck_incident_occurrence_members"),
        sa.CheckConstraint(
            "handling_conclusion IN ('NEW','IN_PROGRESS','CLOSED','FALSE_POSITIVE')",
            name="ck_incident_occurrence_conclusion",
        ),
    )
    op.create_index(
        "ix_incident_occurrence_recovered",
        "incident_occurrence",
        ["recovered_at", "id"],
    )
    op.create_index(
        "ix_incident_occurrence_source_recovered",
        "incident_occurrence",
        ["source_id", "recovered_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_incident_occurrence_source_recovered", table_name="incident_occurrence")
    op.drop_index("ix_incident_occurrence_recovered", table_name="incident_occurrence")
    op.drop_table("incident_occurrence")
    op.drop_index("ix_incident_audit_incident_created", table_name="incident_audit")
    op.drop_table("incident_audit")
    op.drop_index("ix_incident_handling_state", table_name="incident")
    op.drop_column("incident", "change_origin")
    op.drop_column("incident", "change_version")
    op.drop_column("incident", "handling_version")
    op.drop_column("incident", "handling_state")
