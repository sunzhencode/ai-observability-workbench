"""Persist successful metric backfill outcomes for management health.

Revision ID: platform_0028
Revises: platform_0027
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0028"
down_revision: str | None = "platform_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metric_backfill_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id"),
            nullable=False,
        ),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("requested_hours", sa.Integer(), nullable=False),
        sa.Column("effective_hours", sa.Integer(), nullable=False),
        sa.Column("truncated_reason", sa.String(length=96), nullable=True),
        sa.Column("alerts_reconstructed", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_metric_backfill_run_completed",
        "metric_backfill_run",
        ["completed_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_metric_backfill_run_completed", table_name="metric_backfill_run"
    )
    op.drop_table("metric_backfill_run")
