"""Add versioned Prompt Profiles and deterministic selection bindings.

Revision ID: platform_0019
Revises: platform_0018
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0019"
down_revision: str | None = "platform_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_profile",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("active_revision_id", sa.Integer()),
        sa.Column("is_global_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "prompt_profile_revision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "profile_id",
            sa.String(length=128),
            sa.ForeignKey("prompt_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("guidance_json", sa.Text(), nullable=False),
        sa.Column("tested_at", sa.DateTime()),
        sa.Column("last_test_code", sa.String(length=96)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("profile_id", "revision_no", name="uq_prompt_profile_revision"),
    )
    op.create_table(
        "prompt_profile_service_binding",
        sa.Column("service_id", sa.Integer(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.String(length=128),
            sa.ForeignKey("prompt_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("prompt_profile_service_binding")
    op.drop_table("prompt_profile_revision")
    op.drop_table("prompt_profile")
