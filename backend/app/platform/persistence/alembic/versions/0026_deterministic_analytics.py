"""Add deterministic operational Analytics projection facts.

Revision ID: platform_0026
Revises: platform_0025
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0026"
down_revision: str | None = "platform_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operational_occurrence",
        sa.Column("creation_unmapped", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "notification_delivery",
        sa.Column(
            "execution_mode",
            sa.String(length=24),
            nullable=False,
            server_default="UNKNOWN_LEGACY",
        ),
    )
    op.add_column(
        "investigation_run_v2",
        sa.Column(
            "model_execution_mode",
            sa.String(length=24),
            nullable=False,
            server_default="UNKNOWN_LEGACY",
        ),
    )
    op.create_table(
        "analytics_daily_bucket",
        sa.Column("day_utc", sa.String(length=10), primary_key=True),
        sa.Column("source_id", sa.String(length=128), primary_key=True),
        sa.Column("service_key", sa.String(length=32), primary_key=True),
        sa.Column("signal_severity", sa.String(length=16), primary_key=True),
        sa.Column("hours_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_analytics_bucket_dimensions",
        "analytics_daily_bucket",
        ["day_utc", "source_id", "service_key", "signal_severity"],
    )
    op.create_table(
        "analytics_duration_sample",
        sa.Column("fact_key", sa.String(length=160), primary_key=True),
        sa.Column("day_utc", sa.String(length=10), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("service_key", sa.String(length=32), nullable=False),
        sa.Column("signal_severity", sa.String(length=16), nullable=False),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("value_ms", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("value_ms >= 0", name="ck_analytics_duration_nonnegative"),
    )
    op.create_index(
        "ix_analytics_duration_filter",
        "analytics_duration_sample",
        [
            "occurred_at",
            "source_id",
            "service_key",
            "signal_severity",
            "metric",
        ],
    )
    op.create_table(
        "analytics_rollup_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_complete_hour", sa.DateTime(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_analytics_rollup_singleton"),
    )


def downgrade() -> None:
    op.drop_table("analytics_rollup_state")
    op.drop_index("ix_analytics_duration_filter", table_name="analytics_duration_sample")
    op.drop_table("analytics_duration_sample")
    op.drop_index("ix_analytics_bucket_dimensions", table_name="analytics_daily_bucket")
    op.drop_table("analytics_daily_bucket")
    op.drop_column("investigation_run_v2", "model_execution_mode")
    op.drop_column("notification_delivery", "execution_mode")
    op.drop_column("operational_occurrence", "creation_unmapped")
