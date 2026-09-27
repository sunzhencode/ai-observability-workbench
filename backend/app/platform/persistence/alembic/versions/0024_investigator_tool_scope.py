"""Freeze the validated read-only metric tool scope for each V2 run.

Revision ID: platform_0024
Revises: platform_0023
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0024"
down_revision: str | None = "platform_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigation_tool_scope_v2",
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation_run_v2.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=True),
        sa.Column("catalog_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.execute(
        "CREATE TRIGGER investigation_tool_scope_v2_no_update "
        "BEFORE UPDATE ON investigation_tool_scope_v2 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_tool_scope_v2 is append-only'); END"
    )
    op.create_table(
        "investigation_metric_observation_v2",
        sa.Column(
            "evidence_id",
            sa.String(length=96),
            primary_key=True,
        ),
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation_run_v2.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("metric_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("sample_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "investigation_id",
            "evidence_id",
            name="uq_investigation_metric_observation_v2_ref",
        ),
    )
    op.execute(
        "CREATE TRIGGER investigation_metric_observation_v2_no_update "
        "BEFORE UPDATE ON investigation_metric_observation_v2 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_metric_observation_v2 is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER investigation_metric_observation_v2_no_delete "
        "BEFORE DELETE ON investigation_metric_observation_v2 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_metric_observation_v2 is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER investigation_tool_scope_v2_no_delete "
        "BEFORE DELETE ON investigation_tool_scope_v2 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_tool_scope_v2 is append-only'); END"
    )


def downgrade() -> None:
    op.drop_table("investigation_metric_observation_v2")
    op.drop_table("investigation_tool_scope_v2")
