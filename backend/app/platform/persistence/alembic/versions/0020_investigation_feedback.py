"""Add append-only operator feedback for completed investigation results.

Revision ID: platform_0020
Revises: platform_0019
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0020"
down_revision: str | None = "platform_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigation_feedback_v1",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("initiator_kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "investigation_id",
            "sequence",
            name="uq_investigation_feedback_sequence",
        ),
    )
    op.execute(
        "CREATE TRIGGER investigation_feedback_v1_no_update "
        "BEFORE UPDATE ON investigation_feedback_v1 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_feedback_v1 is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER investigation_feedback_v1_no_delete "
        "BEFORE DELETE ON investigation_feedback_v1 "
        "WHEN (SELECT enabled FROM investigation_retention_gate WHERE id=1)=0 "
        "BEGIN SELECT RAISE(ABORT, 'investigation_feedback_v1 is append-only'); END"
    )


def downgrade() -> None:
    op.drop_table("investigation_feedback_v1")
