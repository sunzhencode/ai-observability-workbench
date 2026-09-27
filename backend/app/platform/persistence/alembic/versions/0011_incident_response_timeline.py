"""Add the append-only typed Incident response timeline.

Revision ID: platform_0011
Revises: platform_0010
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0011"
down_revision: str | None = "platform_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operational_occurrence") as batch:
        batch.add_column(
            sa.Column("duplicate_of_occurrence_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_operational_occurrence_duplicate",
            "operational_occurrence",
            ["duplicate_of_occurrence_id"],
            ["id"],
        )
    with op.batch_alter_table("notification_route") as batch:
        batch.add_column(
            sa.Column(
                "current_response_state",
                sa.String(length=24),
                nullable=False,
                server_default="UNACKNOWLEDGED",
            )
        )
        batch.create_check_constraint(
            "ck_notification_route_response",
            "current_response_state IN ('UNACKNOWLEDGED','ACKNOWLEDGED','INVESTIGATING','MITIGATING','MONITORING','RESOLVED')",
        )
    op.create_table(
        "incident_timeline_entry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurrence_id",
            sa.Integer(),
            sa.ForeignKey("operational_occurrence.id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("summary", sa.String(length=512), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("source_ip", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "occurrence_id", "sequence", name="uq_incident_timeline_sequence"
        ),
        sa.CheckConstraint("sequence > 0", name="ck_incident_timeline_sequence"),
        sa.CheckConstraint(
            "actor_type IN ('INTERACTIVE_OPERATOR','SYSTEM')",
            name="ck_incident_timeline_actor",
        ),
    )
    op.create_index(
        "ix_incident_timeline_occurrence",
        "incident_timeline_entry",
        ["occurrence_id", "sequence"],
    )
    op.execute(
        """
        CREATE TRIGGER incident_timeline_no_update
        BEFORE UPDATE ON incident_timeline_entry
        BEGIN
          SELECT RAISE(ABORT, 'INCIDENT_TIMELINE_APPEND_ONLY');
        END
        """
    )
    op.execute(
        """
        UPDATE operational_occurrence
        SET response_state = CASE (
              SELECT handling_state FROM incident
              WHERE incident.id = operational_occurrence.incident_id
            )
              WHEN 'IN_PROGRESS' THEN 'INVESTIGATING'
              WHEN 'CLOSED' THEN 'RESOLVED'
              WHEN 'FALSE_POSITIVE' THEN 'RESOLVED'
              ELSE 'UNACKNOWLEDGED'
            END,
            resolution_code = CASE (
              SELECT handling_state FROM incident
              WHERE incident.id = operational_occurrence.incident_id
            )
              WHEN 'CLOSED' THEN 'FIXED'
              WHEN 'FALSE_POSITIVE' THEN 'FALSE_POSITIVE'
              ELSE NULL
            END,
            acknowledged_at = CASE
              WHEN (SELECT handling_state FROM incident WHERE incident.id = operational_occurrence.incident_id) != 'NEW'
              THEN COALESCE(acknowledged_at, detected_at, latest_activity_at)
              ELSE acknowledged_at
            END,
            first_investigating_at = CASE
              WHEN (SELECT handling_state FROM incident WHERE incident.id = operational_occurrence.incident_id) = 'IN_PROGRESS'
              THEN COALESCE(first_investigating_at, detected_at, latest_activity_at)
              ELSE first_investigating_at
            END,
            resolved_at = CASE
              WHEN (SELECT handling_state FROM incident WHERE incident.id = operational_occurrence.incident_id) IN ('CLOSED','FALSE_POSITIVE')
              THEN COALESCE(resolved_at, latest_activity_at)
              ELSE resolved_at
            END
        """
    )
    op.execute(
        """
        UPDATE notification_route
        SET current_response_state = CASE current_handling_state
              WHEN 'IN_PROGRESS' THEN 'INVESTIGATING'
              WHEN 'CLOSED' THEN 'RESOLVED'
              WHEN 'FALSE_POSITIVE' THEN 'RESOLVED'
              ELSE 'UNACKNOWLEDGED'
            END,
            reminders_paused = CASE current_handling_state WHEN 'NEW' THEN 0 ELSE 1 END
        """
    )
    op.execute(
        """
        INSERT INTO incident_timeline_entry (
          occurrence_id, sequence, actor_type, event_type, summary,
          detail_json, request_id, source_ip, created_at
        )
        SELECT operational_occurrence.id, 1, 'SYSTEM', 'LEGACY_HANDLING_IMPORTED',
          '旧 handling 已确定性导入 Response State',
          '{"legacy_handling_state":"' || incident.handling_state ||
          '","after_response_state":"' || operational_occurrence.response_state || '"}',
          'migration-platform-0011', 'local-migration', operational_occurrence.latest_activity_at
        FROM operational_occurrence
        JOIN incident ON incident.id = operational_occurrence.incident_id
        WHERE incident.handling_state != 'NEW'
        """
    )
    op.execute(
        """
        CREATE TRIGGER incident_timeline_no_delete
        BEFORE DELETE ON incident_timeline_entry
        BEGIN
          SELECT RAISE(ABORT, 'INCIDENT_TIMELINE_APPEND_ONLY');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS incident_timeline_no_delete")
    op.execute("DROP TRIGGER IF EXISTS incident_timeline_no_update")
    op.drop_index(
        "ix_incident_timeline_occurrence", table_name="incident_timeline_entry"
    )
    op.drop_table("incident_timeline_entry")
    with op.batch_alter_table("operational_occurrence") as batch:
        batch.drop_constraint(
            "fk_operational_occurrence_duplicate", type_="foreignkey"
        )
        batch.drop_column("duplicate_of_occurrence_id")
    with op.batch_alter_table("notification_route") as batch:
        batch.drop_constraint("ck_notification_route_response", type_="check")
        batch.drop_column("current_response_state")
