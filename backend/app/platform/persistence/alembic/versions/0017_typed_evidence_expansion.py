"""Add typed Planner trajectory and cooperative cancellation.

Revision ID: platform_0017
Revises: platform_0016
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0017"
down_revision: str | None = "platform_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )


def upgrade() -> None:
    op.add_column("investigation", sa.Column("cancel_requested_at", sa.DateTime(), nullable=True))
    op.add_column("investigation", sa.Column("snapshot_occurrence_version", sa.Integer(), nullable=True))
    op.add_column(
        "investigation",
        sa.Column("planner_rounds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "investigation",
        sa.Column("accounted_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "investigation",
        sa.Column("metric_queries_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("investigation", sa.Column("termination_reason", sa.String(length=96), nullable=True))
    op.create_table(
        "planner_step_v1",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("alert_ref", sa.String(length=32), nullable=True),
        sa.Column("metric_name", sa.String(length=256), nullable=True),
        sa.Column("window", sa.String(length=8), nullable=True),
        sa.Column("aggregation", sa.String(length=16), nullable=True),
        sa.Column("label_filters_json", sa.Text(), nullable=False),
        sa.Column("group_by_json", sa.Text(), nullable=False),
        sa.Column("compiled_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("safe_code", sa.String(length=96), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("result_metric_names_json", sa.Text(), nullable=False),
        sa.Column("descriptor_type", sa.String(length=32), nullable=True),
        sa.Column("descriptor_help", sa.String(length=1024), nullable=True),
        sa.Column("descriptor_unit", sa.String(length=64), nullable=True),
        sa.Column("descriptor_label_names_json", sa.Text(), nullable=False),
        sa.Column("descriptor_known_values_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "investigation_id", "sequence", name="uq_planner_step_sequence"
        ),
    )
    op.create_table(
        "planner_label_rejection_v1",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("alert_ref", sa.String(length=32), nullable=False),
        sa.Column("label_name", sa.String(length=128), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "investigation_canceled_v1",
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("reason", sa.String(length=96), nullable=False),
        sa.Column("canceled_before", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for table in (
        "planner_step_v1",
        "planner_label_rejection_v1",
        "investigation_canceled_v1",
    ):
        _append_only(table)


def downgrade() -> None:
    op.drop_table("investigation_canceled_v1")
    op.drop_table("planner_label_rejection_v1")
    op.drop_table("planner_step_v1")
    op.drop_column("investigation", "termination_reason")
    op.drop_column("investigation", "accounted_tokens")
    op.drop_column("investigation", "metric_queries_total")
    op.drop_column("investigation", "planner_rounds")
    op.drop_column("investigation", "snapshot_occurrence_version")
    op.drop_column("investigation", "cancel_requested_at")
