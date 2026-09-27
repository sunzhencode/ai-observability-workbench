"""Add bounded Incident tasks and encrypted manual-note content.

Revision ID: platform_0012
Revises: platform_0011
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0012"
down_revision: str | None = "platform_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_task",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurrence_id",
            sa.Integer(),
            sa.ForeignKey("operational_occurrence.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("runbook_link", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('TODO','IN_PROGRESS','DONE','CANCELED')",
            name="ck_incident_task_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_incident_task_version"),
        sa.CheckConstraint(
            "runbook_link IS NULL OR runbook_link LIKE 'https://%'",
            name="ck_incident_task_runbook_https",
        ),
    )
    op.create_index(
        "ix_incident_task_occurrence_status",
        "incident_task",
        ["occurrence_id", "status", "id"],
    )
    op.create_table(
        "incident_note_content",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurrence_id",
            sa.Integer(),
            sa.ForeignKey("operational_occurrence.id"),
            nullable=False,
        ),
        sa.Column(
            "timeline_entry_id",
            sa.Integer(),
            sa.ForeignKey("incident_timeline_entry.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("content_envelope", sa.Text(), nullable=True),
        sa.Column("redacted_at", sa.DateTime(), nullable=True),
        sa.Column("purge_after", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "redacted_at IS NULL OR purge_after IS NOT NULL",
            name="ck_incident_note_redaction_purge",
        ),
    )
    op.create_index(
        "ix_incident_note_purge",
        "incident_note_content",
        ["purge_after", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_incident_note_purge", table_name="incident_note_content")
    op.drop_table("incident_note_content")
    op.drop_index(
        "ix_incident_task_occurrence_status", table_name="incident_task"
    )
    op.drop_table("incident_task")
