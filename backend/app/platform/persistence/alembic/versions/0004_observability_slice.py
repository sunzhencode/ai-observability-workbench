"""Add isolated Incident Operations metrics, Grafana and model-channel candidate tables.

Revision ID: platform_0004
Revises: platform_0003
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "platform_0004"
down_revision: str | None = "platform_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alert",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="LIVE_POLL"),
    )
    op.add_column(
        "alert",
        sa.Column(
            "evidence_completeness",
            sa.String(length=24),
            nullable=False,
            server_default="COMPLETE",
        ),
    )
    op.create_table(
        "monitoring_connection",
        sa.Column("source_id", sa.String(length=128), sa.ForeignKey("event_source.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(length=16), primary_key=True),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("secret_envelope", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("tested_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_code", sa.String(length=96), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('THANOS','GRAFANA')", name="ck_monitoring_connection_kind"),
        sa.CheckConstraint("state IN ('DRAFT','ACTIVE')", name="ck_monitoring_connection_state"),
    )
    op.create_table(
        "metric_template",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("promql", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("required_labels_json", sa.Text(), nullable=False),
        sa.Column("legend_format", sa.String(length=256), nullable=False),
        sa.Column("unit", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("source_ids_json", sa.Text(), nullable=False),
        sa.Column("origin_kind", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("origin_kind IN ('MANUAL','GRAFANA')", name="ck_metric_template_origin"),
    )
    op.create_index("ix_metric_template_selection", "metric_template", ["enabled", "priority", "id"])
    op.create_table(
        "grafana_template_origin",
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("metric_template.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_id", sa.String(length=128), sa.ForeignKey("event_source.id"), nullable=False),
        sa.Column("dashboard_uid", sa.String(length=64), nullable=False),
        sa.Column("dashboard_title", sa.String(length=256), nullable=False),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("panel_title", sa.String(length=256), nullable=False),
        sa.Column("ref_id", sa.String(length=32), nullable=False),
        sa.Column("imported_promql", sa.Text(), nullable=False),
        sa.Column("confirmed_promql", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "source_id",
            "dashboard_uid",
            "panel_id",
            "ref_id",
            name="uq_grafana_template_origin_target",
        ),
    )
    op.create_table(
        "model_channel",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("active_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "model_channel_revision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(length=128), sa.ForeignKey("model_channel.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("api_key_envelope", sa.Text(), nullable=True),
        sa.Column("tested_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("channel_id", "revision_no", name="uq_model_channel_revision"),
    )
    op.create_index("ix_model_channel_revision_state", "model_channel_revision", ["channel_id", "state", "revision_no"])
    op.create_table(
        "model_channel_audit",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(length=128), sa.ForeignKey("model_channel.id"), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("model_channel_audit")
    op.drop_index("ix_model_channel_revision_state", table_name="model_channel_revision")
    op.drop_table("model_channel_revision")
    op.drop_table("model_channel")
    op.drop_table("grafana_template_origin")
    op.drop_index("ix_metric_template_selection", table_name="metric_template")
    op.drop_table("metric_template")
    op.drop_table("monitoring_connection")
    op.drop_column("alert", "evidence_completeness")
    op.drop_column("alert", "origin")
