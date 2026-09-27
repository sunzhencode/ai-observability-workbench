"""Add frozen scope and collection freshness to operational occurrences.

Revision ID: platform_0009
Revises: platform_0008
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0009"
down_revision: str | None = "platform_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operational_occurrence") as batch:
        batch.add_column(
            sa.Column("group_key", sa.String(length=512), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "evidence_completeness",
                sa.String(length=16),
                nullable=False,
                server_default="UNKNOWN",
            )
        )
        batch.create_check_constraint(
            "ck_operational_occurrence_member_count", "member_count >= 0"
        )
        batch.create_check_constraint(
            "ck_operational_occurrence_completeness",
            "evidence_completeness IN ('COMPLETE','PARTIAL','FAILED','UNKNOWN')",
        )


def downgrade() -> None:
    with op.batch_alter_table("operational_occurrence") as batch:
        batch.drop_constraint(
            "ck_operational_occurrence_completeness", type_="check"
        )
        batch.drop_constraint(
            "ck_operational_occurrence_member_count", type_="check"
        )
        batch.drop_column("evidence_completeness")
        batch.drop_column("member_count")
        batch.drop_column("group_key")
