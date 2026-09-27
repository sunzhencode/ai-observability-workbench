"""Add secret-free receipts for idempotent operator commands.

Revision ID: platform_0010
Revises: platform_0009
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0010"
down_revision: str | None = "platform_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_receipt",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=96), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("scope", "key_hash", name="uq_command_receipt_scope_key"),
        sa.CheckConstraint(
            "state IN ('IN_PROGRESS','COMPLETED')",
            name="ck_command_receipt_state",
        ),
    )


def downgrade() -> None:
    op.drop_table("command_receipt")
