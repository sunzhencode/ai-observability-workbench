"""Add deterministic notification-noise controls and state.

Revision ID: platform_0015
Revises: platform_0014
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0015"
down_revision: str | None = "platform_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "aggregation_rule",
        sa.Column(
            "grouping_window_seconds",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.create_table(
        "source_noise_control",
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("flapping_enabled", sa.Boolean(), nullable=False),
        sa.Column("storm_enabled", sa.Boolean(), nullable=False),
        sa.Column("storm_alert_threshold", sa.Integer(), nullable=False),
        sa.Column("storm_occurrence_threshold", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "storm_alert_threshold BETWEEN 10 AND 10000",
            name="ck_source_noise_alert_threshold",
        ),
        sa.CheckConstraint(
            "storm_occurrence_threshold BETWEEN 5 AND 1000",
            name="ck_source_noise_occurrence_threshold",
        ),
        sa.CheckConstraint("version > 0", name="ck_source_noise_version"),
    )
    op.create_table(
        "alert_flapping_state",
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("fingerprint", sa.String(length=256), primary_key=True),
        sa.Column("last_stable_state", sa.String(length=16), nullable=False),
        sa.Column("transitions_json", sa.Text(), nullable=False),
        sa.Column("flapping_since", sa.DateTime(), nullable=True),
        sa.Column("last_transition_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_alert_flapping_active",
        "alert_flapping_state",
        ["source_id", "flapping_since"],
    )
    op.create_table(
        "source_storm_state",
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("active_since", sa.DateTime(), nullable=True),
        sa.Column("below_half_windows", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("samples_json", sa.Text(), nullable=False),
        sa.Column("new_alert_count", sa.Integer(), nullable=False),
        sa.Column("new_occurrence_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "storm_notification_summary",
        sa.Column("summary_key", sa.String(length=512), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_revision_id",
            sa.Integer(),
            sa.ForeignKey("notification_policy_revision.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_set_json", sa.Text(), nullable=False),
        sa.Column("route_target_id", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_storm_summary_window",
        "storm_notification_summary",
        ["source_id", "window_started_at"],
    )
    op.create_table(
        "maintenance_window",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "source_id",
            sa.String(length=128),
            sa.ForeignKey("event_source.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("service.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "aggregation_rule_id",
            sa.Integer(),
            sa.ForeignKey("aggregation_rule.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(scope_kind='SOURCE' AND source_id IS NOT NULL AND service_id IS NULL AND aggregation_rule_id IS NULL) OR "
            "(scope_kind='SERVICE' AND source_id IS NULL AND service_id IS NOT NULL AND aggregation_rule_id IS NULL) OR "
            "(scope_kind='RULE' AND source_id IS NULL AND service_id IS NULL AND aggregation_rule_id IS NOT NULL)",
            name="ck_maintenance_scope",
        ),
        sa.CheckConstraint("status IN ('ACTIVE','ENDED')", name="ck_maintenance_status"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_maintenance_time"),
    )
    op.create_index(
        "ix_maintenance_active_window",
        "maintenance_window",
        ["status", "starts_at", "ends_at", "id"],
    )
    op.create_table(
        "occurrence_suppression",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurrence_id",
            sa.Integer(),
            sa.ForeignKey("operational_occurrence.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','ENDED')", name="ck_suppression_status"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_suppression_time"),
    )
    op.create_index(
        "ix_suppression_occurrence_active",
        "occurrence_suppression",
        ["occurrence_id", "status", "ends_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_suppression_occurrence_active", table_name="occurrence_suppression")
    op.drop_table("occurrence_suppression")
    op.drop_index("ix_maintenance_active_window", table_name="maintenance_window")
    op.drop_table("maintenance_window")
    op.drop_index("ix_storm_summary_window", table_name="storm_notification_summary")
    op.drop_table("storm_notification_summary")
    op.drop_table("source_storm_state")
    op.drop_index("ix_alert_flapping_active", table_name="alert_flapping_state")
    op.drop_table("alert_flapping_state")
    op.drop_table("source_noise_control")
    op.drop_column("aggregation_rule", "grouping_window_seconds")
