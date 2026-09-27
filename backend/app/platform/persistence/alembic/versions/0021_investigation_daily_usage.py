"""Add platform-only daily investigation usage rollups.

Revision ID: platform_0021
Revises: platform_0020
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0021"
down_revision: str | None = "platform_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigation",
        sa.Column(
            "model_execution_mode",
            sa.String(length=16),
            nullable=False,
            server_default="UNKNOWN_LEGACY",
        ),
    )
    op.create_index(
        "ix_investigation_created_at",
        "investigation",
        ["created_at"],
    )
    op.create_table(
        "investigation_daily_usage_v1",
        sa.Column("day_utc", sa.String(length=10), primary_key=True),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("terminal_run_count", sa.Integer(), nullable=False),
        sa.Column("p2_valid_count", sa.Integer(), nullable=False),
        sa.Column("evidence_only_count", sa.Integer(), nullable=False),
        sa.Column("canceled_count", sa.Integer(), nullable=False),
        sa.Column("contract_rejected_count", sa.Integer(), nullable=False),
        sa.Column("dependency_failed_count", sa.Integer(), nullable=False),
        sa.Column("model_started_run_count", sa.Integer(), nullable=False),
        sa.Column("fake_model_started_run_count", sa.Integer(), nullable=False),
        sa.Column("external_model_started_run_count", sa.Integer(), nullable=False),
        sa.Column("unknown_model_started_run_count", sa.Integer(), nullable=False),
        sa.Column("planner_calls", sa.Integer(), nullable=False),
        sa.Column("analyst_calls", sa.Integer(), nullable=False),
        sa.Column("metric_queries", sa.Integer(), nullable=False),
        sa.Column("accounted_tokens", sa.Integer(), nullable=False),
        sa.Column("unknown_cost_run_count", sa.Integer(), nullable=False),
        sa.Column("feedback_response_count", sa.Integer(), nullable=False),
        sa.Column("feedback_adopted_count", sa.Integer(), nullable=False),
        sa.Column("degraded_run_count", sa.Integer(), nullable=False),
        sa.Column("p0_mtti_count", sa.Integer(), nullable=False),
        sa.Column("p0_mtti_sum_ms", sa.Integer(), nullable=False),
        sa.Column("p1_mtti_count", sa.Integer(), nullable=False),
        sa.Column("p1_mtti_sum_ms", sa.Integer(), nullable=False),
        sa.Column("p2_mtti_count", sa.Integer(), nullable=False),
        sa.Column("p2_mtti_sum_ms", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("investigation_daily_usage_v1")
    op.drop_index("ix_investigation_created_at", table_name="investigation")
    op.drop_column("investigation", "model_execution_mode")
