"""Freeze occurrence matching facts and add typed similar-history observations.

Revision ID: platform_0022
Revises: platform_0021
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0022"
down_revision: str | None = "platform_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operational_occurrence",
        sa.Column("primary_alertname", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "operational_occurrence",
        sa.Column("aggregation_rule_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "operational_occurrence",
        sa.Column(
            "group_labels_json", sa.Text(), nullable=False, server_default="{}"
        ),
    )
    op.execute(
        "UPDATE operational_occurrence SET "
        "primary_alertname=(SELECT alert.alertname FROM alert "
        "WHERE alert.incident_id=operational_occurrence.incident_id "
        "GROUP BY alert.alertname ORDER BY count(*) DESC, alert.alertname LIMIT 1), "
        "aggregation_rule_id=(SELECT incident.aggregation_rule_id FROM incident "
        "WHERE incident.id=operational_occurrence.incident_id), "
        "group_labels_json=coalesce((SELECT incident.group_labels_json FROM incident "
        "WHERE incident.id=operational_occurrence.incident_id), '{}')"
    )
    op.create_table(
        "similar_history_observation_v1",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("match_reasons_json", sa.Text(), nullable=False),
        sa.Column("resolution_code", sa.String(length=24), nullable=False),
        sa.Column("operator_conclusion", sa.Text(), nullable=True),
        sa.Column("task_outcome", sa.Text(), nullable=True),
        sa.Column("handling_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "investigation_id", "rank", name="uq_similar_history_observation_rank"
        ),
    )
    op.execute(
        "CREATE TRIGGER similar_history_observation_v1_no_update BEFORE UPDATE ON "
        "similar_history_observation_v1 WHEN (SELECT enabled FROM "
        "investigation_retention_gate WHERE id=1)=0 BEGIN SELECT RAISE(ABORT, "
        "'similar_history_observation_v1 is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER similar_history_observation_v1_no_delete BEFORE DELETE ON "
        "similar_history_observation_v1 WHEN (SELECT enabled FROM "
        "investigation_retention_gate WHERE id=1)=0 BEGIN SELECT RAISE(ABORT, "
        "'similar_history_observation_v1 is append-only'); END"
    )


def downgrade() -> None:
    op.drop_table("similar_history_observation_v1")
    op.drop_column("operational_occurrence", "group_labels_json")
    op.drop_column("operational_occurrence", "aggregation_rule_id")
    op.drop_column("operational_occurrence", "primary_alertname")
