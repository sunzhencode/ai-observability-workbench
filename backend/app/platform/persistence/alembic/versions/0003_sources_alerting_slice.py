"""Add isolated Incident Operations source and alerting candidate tables.

Revision ID: platform_0003
Revises: platform_0002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "platform_0003"
down_revision: str | None = "platform_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_source",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("resolution_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("max_parallel_endpoints", sa.Integer(), nullable=False),
        sa.Column("watchdog_enabled", sa.Boolean(), nullable=False),
        sa.Column("watchdog_alertname", sa.String(length=128), nullable=False),
        sa.Column("watchdog_identity_label", sa.String(length=128), nullable=False),
        sa.Column("watchdog_missing_after_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('ENABLED','DISABLED','ARCHIVED')",
            name="ck_event_source_state",
        ),
    )
    op.create_table(
        "source_endpoint",
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auth_kind", sa.String(length=16), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=False),
        sa.Column("secret_envelope", sa.Text(), nullable=True),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "source_id", "canonical_url", name="uq_source_endpoint_url"
        ),
    )
    op.create_table(
        "source_audit",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_source_audit_source_sequence",
        "source_audit",
        ["source_id", "sequence"],
    )
    op.create_table(
        "source_poll_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id"),
            nullable=False,
        ),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("completeness", sa.String(length=16), nullable=False),
        sa.Column("endpoint_total", sa.Integer(), nullable=False),
        sa.Column("endpoint_succeeded", sa.Integer(), nullable=False),
        sa.Column("alert_count", sa.Integer(), nullable=False),
        sa.Column("safe_error_codes_json", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_source_poll_run_source_started",
        "source_poll_run",
        ["source_id", "started_at"],
    )
    op.create_table(
        "endpoint_poll_result",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "poll_run_id",
            sa.Integer(),
            sa.ForeignKey("source_poll_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint_position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("alert_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("safe_error_code", sa.String(length=96), nullable=True),
    )
    op.create_table(
        "aggregation_rule",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("matchers_json", sa.Text(), nullable=False),
        sa.Column("group_by_labels_json", sa.Text(), nullable=False),
        sa.Column("source_ids_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_aggregation_rule_order",
        "aggregation_rule",
        ["enabled", "priority", "id"],
    )
    op.create_table(
        "incident",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id"),
            nullable=False,
        ),
        sa.Column("group_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("source_state", sa.String(length=24), nullable=False),
        sa.Column("freshness_state", sa.String(length=16), nullable=False),
        sa.Column("aggregation_rule_id", sa.Integer(), nullable=True),
        sa.Column("aggregation_rule_version", sa.Integer(), nullable=True),
        sa.Column("group_labels_json", sa.Text(), nullable=False),
        sa.Column("missing_labels_json", sa.Text(), nullable=False),
        sa.Column("occurrence_no", sa.Integer(), nullable=False),
        sa.Column("occurrence_started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source_id", "group_key", name="uq_incident_group"),
    )
    op.create_index(
        "ix_incident_source_state",
        "incident",
        ["source_id", "source_state", "updated_at"],
    )
    op.create_table(
        "alert",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id"),
            nullable=False,
        ),
        sa.Column("upstream_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("alertname", sa.String(length=256), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("cluster", sa.String(length=256), nullable=False),
        sa.Column("labels_json", sa.Text(), nullable=False),
        sa.Column("annotations_json", sa.Text(), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("source_state", sa.String(length=24), nullable=False),
        sa.Column("missing_since_at", sa.DateTime(), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("endpoint_positions_json", sa.Text(), nullable=False),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incident.id"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_id", "upstream_fingerprint", name="uq_alert_identity"
        ),
    )
    op.create_index(
        "ix_alert_source_state",
        "alert",
        ["source_id", "source_state", "last_seen_at"],
    )
    op.create_table(
        "watchdog_cluster",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id"),
            nullable=False,
        ),
        sa.Column("identity_value", sa.String(length=256), nullable=False),
        sa.Column("inventory_state", sa.String(length=16), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "source_id", "identity_value", name="uq_watchdog_cluster_identity"
        ),
    )


def downgrade() -> None:
    op.drop_table("watchdog_cluster")
    op.drop_index("ix_alert_source_state", table_name="alert")
    op.drop_table("alert")
    op.drop_index("ix_incident_source_state", table_name="incident")
    op.drop_table("incident")
    op.drop_index("ix_aggregation_rule_order", table_name="aggregation_rule")
    op.drop_table("aggregation_rule")
    op.drop_table("endpoint_poll_result")
    op.drop_index(
        "ix_source_poll_run_source_started", table_name="source_poll_run"
    )
    op.drop_table("source_poll_run")
    op.drop_index("ix_source_audit_source_sequence", table_name="source_audit")
    op.drop_table("source_audit")
    op.drop_table("source_endpoint")
    op.drop_table("event_source")
