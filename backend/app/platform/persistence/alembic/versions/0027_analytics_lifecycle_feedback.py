"""Persist analytics lifecycle facts and V2 investigation feedback.

Revision ID: platform_0027
Revises: platform_0026
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0027"
down_revision: str | None = "platform_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "noise_lifecycle_fact",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fact_key", sa.String(length=640), nullable=False, unique=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("transition", sa.String(length=16), nullable=False),
        sa.Column("subject_key", sa.String(length=512), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("service_key", sa.String(length=32), nullable=False),
        sa.Column("signal_severity", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('FLAPPING','STORM')", name="ck_noise_lifecycle_kind"
        ),
        sa.CheckConstraint(
            "transition IN ('ACTIVATED','CLEARED')",
            name="ck_noise_lifecycle_transition",
        ),
    )
    op.create_index(
        "ix_noise_lifecycle_analytics",
        "noise_lifecycle_fact",
        ["occurred_at", "source_id", "service_key", "signal_severity", "kind"],
    )
    op.create_table(
        "investigation_feedback_v2",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("investigation_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "investigation_id",
            "sequence",
            name="uq_investigation_feedback_v2_sequence",
        ),
        sa.CheckConstraint(
            "rating IN ('USEFUL','NOT_USEFUL','ADOPTED')",
            name="ck_investigation_feedback_v2_rating",
        ),
    )
    op.create_index(
        "ix_investigation_feedback_v2_latest",
        "investigation_feedback_v2",
        ["investigation_id", "sequence"],
    )
    op.execute(
        "CREATE TRIGGER investigation_feedback_v2_no_update "
        "BEFORE UPDATE ON investigation_feedback_v2 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_feedback_v2 is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER investigation_feedback_v2_no_delete "
        "BEFORE DELETE ON investigation_feedback_v2 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_feedback_v2 is append-only'); END"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investigation_feedback_v2_latest",
        table_name="investigation_feedback_v2",
    )
    op.drop_table("investigation_feedback_v2")
    op.drop_index("ix_noise_lifecycle_analytics", table_name="noise_lifecycle_fact")
    op.drop_table("noise_lifecycle_fact")
