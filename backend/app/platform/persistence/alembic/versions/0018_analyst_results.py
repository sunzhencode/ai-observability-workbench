"""Persist validated Analyst results and encrypted rejected responses.

Revision ID: platform_0018
Revises: platform_0017
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0018"
down_revision: str | None = "platform_0017"
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
    op.add_column(
        "investigation",
        sa.Column("analyst_calls", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "investigation",
        sa.Column("analyst_prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "investigation",
        sa.Column("analyst_completion_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "analyst_result_v1",
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("contract_revision", sa.Integer(), nullable=False),
        sa.Column("prompt_profile_revision", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "invalid_analyst_response",
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("purge_after", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    _append_only("analyst_result_v1")
    _append_only("invalid_analyst_response")


def downgrade() -> None:
    op.drop_table("invalid_analyst_response")
    op.drop_table("analyst_result_v1")
    op.drop_column("investigation", "analyst_completion_tokens")
    op.drop_column("investigation", "analyst_prompt_tokens")
    op.drop_column("investigation", "analyst_calls")
