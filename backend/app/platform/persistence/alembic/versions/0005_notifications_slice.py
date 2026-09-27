"""Add notification configuration, route snapshot, Outbox and attempt tables.

Revision ID: platform_0005
Revises: platform_0004
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0005"
down_revision: str | None = "platform_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_channel",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("active_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('FEISHU_CUSTOM_BOT','SMTP','GENERIC_WEBHOOK')",
            name="ck_notification_channel_provider",
        ),
    )
    op.create_table(
        "notification_channel_revision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(length=128), sa.ForeignKey("notification_channel.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("secret_envelopes_json", sa.Text(), nullable=False),
        sa.Column("tested_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("channel_id", "revision_no", name="uq_notification_channel_revision"),
        sa.CheckConstraint("state IN ('DRAFT','ACTIVE','RETIRED')", name="ck_notification_channel_revision_state"),
    )
    op.create_index("ix_notification_channel_revision_state", "notification_channel_revision", ["channel_id", "state", "revision_no"])
    op.create_table(
        "notification_channel_audit",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(length=128), sa.ForeignKey("notification_channel.id"), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "notification_policy_revision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("logical_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("matchers_json", sa.Text(), nullable=False),
        sa.Column("scope_mode", sa.String(length=16), nullable=False),
        sa.Column("source_ids_json", sa.Text(), nullable=False),
        sa.Column("repeat_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("logical_id", "version", name="uq_notification_policy_revision"),
        sa.CheckConstraint("state IN ('DRAFT','ACTIVE','RETIRED')", name="ck_notification_policy_state"),
        sa.CheckConstraint("scope_mode IN ('ALL','SELECTED')", name="ck_notification_policy_scope"),
        sa.CheckConstraint("repeat_interval_seconds = 0 OR repeat_interval_seconds >= 300", name="ck_notification_repeat_minimum"),
    )
    op.create_index("ix_notification_policy_selection", "notification_policy_revision", ["state", "priority", "id"])
    op.create_table(
        "notification_policy_channel",
        sa.Column("policy_revision_id", sa.Integer(), sa.ForeignKey("notification_policy_revision.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("channel_id", sa.String(length=128), sa.ForeignKey("notification_channel.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_table(
        "notification_policy_audit",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("logical_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.Integer(), sa.ForeignKey("notification_policy_revision.id"), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "notification_route",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("occurrence_no", sa.Integer(), nullable=False),
        sa.Column("policy_revision_id", sa.Integer(), sa.ForeignKey("notification_policy_revision.id"), nullable=False),
        sa.Column("policy_name", sa.String(length=120), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("policy_priority", sa.Integer(), nullable=False),
        sa.Column("match_context_json", sa.Text(), nullable=False),
        sa.Column("repeat_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_source_state", sa.String(length=24), nullable=False),
        sa.Column("current_freshness_state", sa.String(length=16), nullable=False),
        sa.Column("current_handling_state", sa.String(length=24), nullable=False),
        sa.Column("reminders_paused", sa.Boolean(), nullable=False),
        sa.Column("last_notified_severity", sa.String(length=16), nullable=True),
        sa.Column("last_successful_event_at", sa.DateTime(), nullable=True),
        sa.Column("next_reminder_at", sa.DateTime(), nullable=True),
        sa.Column("repeat_slot", sa.Integer(), nullable=False),
        sa.Column("termination_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("incident_id", "occurrence_no", name="uq_notification_route_occurrence"),
        sa.CheckConstraint("status IN ('ACTIVE','RECOVERED','TERMINATED')", name="ck_notification_route_status"),
    )
    op.create_index("ix_notification_route_reminder", "notification_route", ["status", "next_reminder_at", "id"])
    op.create_table(
        "notification_route_target",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("notification_route.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(length=128), sa.ForeignKey("notification_channel.id"), nullable=False),
        sa.Column("routed_channel_revision_id", sa.Integer(), sa.ForeignKey("notification_channel_revision.id"), nullable=False),
        sa.Column("channel_name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("channel_revision_no", sa.Integer(), nullable=False),
        sa.Column("opened_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("route_id", "channel_id", name="uq_notification_route_channel"),
    )
    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("occurrence_no", sa.Integer(), nullable=False),
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("notification_route.id"), nullable=False),
        sa.Column("route_target_id", sa.Integer(), sa.ForeignKey("notification_route_target.id"), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("incident_change_version", sa.Integer(), nullable=False),
        sa.Column("repeat_slot", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("payload_snapshot_json", sa.Text(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_trigger", sa.String(length=16), nullable=False),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("suppression_reason", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING','IN_FLIGHT','RETRY_WAIT','SUCCEEDED','PERMANENTLY_FAILED','SUPPRESSED','CANCELED')",
            name="ck_notification_delivery_state",
        ),
        sa.CheckConstraint(
            "event_type IN ('FIRING_OPENED','SEVERITY_ESCALATED','REMINDER','RECOVERED')",
            name="ck_notification_delivery_event",
        ),
    )
    op.create_index("ix_notification_delivery_due", "notification_delivery", ["state", "next_attempt_at", "id"])
    op.create_index("ix_notification_delivery_route", "notification_delivery", ["route_id", "created_at", "id"])
    op.create_table(
        "notification_attempt",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("delivery_id", sa.Integer(), sa.ForeignKey("notification_delivery.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.UniqueConstraint("delivery_id", "attempt_no", name="uq_notification_delivery_attempt"),
    )
    op.create_index("ix_notification_attempt_started", "notification_attempt", ["started_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_notification_attempt_started", table_name="notification_attempt")
    op.drop_table("notification_attempt")
    op.drop_index("ix_notification_delivery_route", table_name="notification_delivery")
    op.drop_index("ix_notification_delivery_due", table_name="notification_delivery")
    op.drop_table("notification_delivery")
    op.drop_table("notification_route_target")
    op.drop_index("ix_notification_route_reminder", table_name="notification_route")
    op.drop_table("notification_route")
    op.drop_table("notification_policy_audit")
    op.drop_table("notification_policy_channel")
    op.drop_index("ix_notification_policy_selection", table_name="notification_policy_revision")
    op.drop_table("notification_policy_revision")
    op.drop_table("notification_channel_audit")
    op.drop_index("ix_notification_channel_revision_state", table_name="notification_channel_revision")
    op.drop_table("notification_channel_revision")
    op.drop_table("notification_channel")
