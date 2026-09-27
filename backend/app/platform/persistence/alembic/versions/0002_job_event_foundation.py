"""Add the Incident Operations durable Job queue and resumable event log.

Revision ID: platform_0002
Revises: platform_0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "platform_0002"
down_revision: str | None = "platform_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_job",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("pool", sa.String(length=24), nullable=False),
        sa.Column("subject_type", sa.String(length=80), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("safe_error_code", sa.String(length=96), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "pool IN ('SOURCE','AI','NOTIFICATION')",
            name="ck_platform_job_pool",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELED')",
            name="ck_platform_job_state",
        ),
        sa.UniqueConstraint("kind", "idempotency_key", name="uq_platform_job_kind_key"),
    )
    op.create_index(
        "ix_platform_job_claim",
        "platform_job",
        ["pool", "state", "available_at", "created_at"],
    )
    op.create_index(
        "ix_platform_job_lease",
        "platform_job",
        ["state", "lease_expires_at"],
    )
    op.create_table(
        "platform_event",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("subject_type", sa.String(length=80), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_platform_event_type_sequence",
        "platform_event",
        ["event_type", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_platform_event_type_sequence", table_name="platform_event")
    op.drop_table("platform_event")
    op.drop_index("ix_platform_job_lease", table_name="platform_job")
    op.drop_index("ix_platform_job_claim", table_name="platform_job")
    op.drop_table("platform_job")
