"""Persist platform settings required by the single-origin frontend.

Revision ID: platform_0007
Revises: platform_0006
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0007"
down_revision: str | None = "platform_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_setting",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    with op.batch_alter_table("metric_template") as batch_op:
        batch_op.add_column(
            sa.Column("builtin_key", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "user_modified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_unique_constraint(
            "uq_metric_template_builtin", ("builtin_key",)
        )


def downgrade() -> None:
    with op.batch_alter_table("metric_template") as batch_op:
        batch_op.drop_constraint("uq_metric_template_builtin", type_="unique")
        batch_op.drop_column("user_modified")
        batch_op.drop_column("builtin_key")
    op.drop_table("platform_setting")
