"""Add provider profiles and V2 investigation product facts.

Revision ID: platform_0023
Revises: platform_0022
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0023"
down_revision: str | None = "platform_0022"
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
    op.create_table(
        "provider_profile",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("provider_id", sa.String(length=32), nullable=False),
        sa.Column("protocol_profile", sa.String(length=32), nullable=False),
        sa.Column("support_level", sa.String(length=24), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "provider_id IN ('OPENAI','DEEPSEEK','MOONSHOT','ZHIPU','DASHSCOPE','CUSTOM')",
            name="ck_provider_profile_provider",
        ),
        sa.CheckConstraint(
            "protocol_profile IN ('RESPONSES','CHAT_COMPLETIONS')",
            name="ck_provider_profile_protocol",
        ),
        sa.CheckConstraint(
            "support_level IN ('REVIEWED','COMPATIBLE','BEST_EFFORT')",
            name="ck_provider_profile_support",
        ),
        sa.UniqueConstraint("provider_id", "revision", name="uq_provider_profile_revision"),
    )
    op.add_column(
        "model_channel_revision",
        sa.Column(
            "provider_profile_id",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_table(
        "investigation_run_v2",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "occurrence_id",
            sa.Integer(),
            sa.ForeignKey("operational_occurrence.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column(
            "provider_profile_id",
            sa.String(length=64),
            sa.ForeignKey("provider_profile.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("model_channel_id", sa.String(length=128), nullable=True),
        sa.Column("model_revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("source_ip", sa.String(length=128), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_error_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("occurrence_id", "request_key", name="uq_investigation_run_v2_request"),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','COMPLETED','DEGRADED','FAILED','CANCELED')",
            name="ck_investigation_run_v2_status",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND tool_call_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name="ck_investigation_run_v2_usage",
        ),
    )
    op.create_index(
        "ix_investigation_run_v2_occurrence_created",
        "investigation_run_v2",
        ["occurrence_id", "created_at", "id"],
    )
    op.create_table(
        "evidence_snapshot_v2",
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation_run_v2.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("alert_evidence_json", sa.Text(), nullable=False),
        sa.Column("metric_evidence_json", sa.Text(), nullable=False),
        sa.Column("degraded_domains_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "investigation_activity_v2",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation_run_v2.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("safe_code", sa.String(length=96), nullable=False),
        sa.Column("evidence_ids_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("investigation_id", "sequence", name="uq_investigation_activity_v2_sequence"),
    )
    op.create_table(
        "investigation_report_v2",
        sa.Column(
            "investigation_id",
            sa.String(length=36),
            sa.ForeignKey("investigation_run_v2.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for table in (
        "evidence_snapshot_v2",
        "investigation_activity_v2",
        "investigation_report_v2",
    ):
        _append_only(table)


def downgrade() -> None:
    op.drop_table("investigation_report_v2")
    op.drop_table("investigation_activity_v2")
    op.drop_table("evidence_snapshot_v2")
    op.drop_index(
        "ix_investigation_run_v2_occurrence_created",
        table_name="investigation_run_v2",
    )
    op.drop_table("investigation_run_v2")
    op.drop_column("model_channel_revision", "provider_profile_id")
    op.drop_table("provider_profile")
