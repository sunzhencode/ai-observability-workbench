"""Add frozen investigation scope and typed P0 evidence.

Revision ID: platform_0016
Revises: platform_0015
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0016"
down_revision: str | None = "platform_0015"
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
        "investigation_retention_gate",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id=1", name="ck_investigation_retention_gate_id"),
    )
    op.execute("INSERT INTO investigation_retention_gate (id,enabled) VALUES (1,0)")
    op.create_table(
        "investigation",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("occurrence_id", sa.Integer(), sa.ForeignKey("operational_occurrence.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incident.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=48), nullable=False),
        sa.Column("member_scope_json", sa.Text(), nullable=False),
        sa.Column("member_total", sa.Integer(), nullable=False),
        sa.Column("member_detailed", sa.Integer(), nullable=False),
        sa.Column("query_planned", sa.Integer(), nullable=False),
        sa.Column("query_completed", sa.Integer(), nullable=False),
        sa.Column("successful_metric_facts", sa.Integer(), nullable=False),
        sa.Column("empty_metric_facts", sa.Integer(), nullable=False),
        sa.Column("degraded_domains_json", sa.Text(), nullable=False),
        sa.Column("findings_json", sa.Text(), nullable=False),
        sa.Column("job_id", sa.String(length=36), sa.ForeignKey("platform_job.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("model_cost_status", sa.String(length=16), nullable=False),
        sa.Column("claim_owner", sa.String(length=128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("source_ip", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("occurrence_id", "request_key", name="uq_investigation_request"),
        sa.CheckConstraint("status IN ('PREPARING','EVIDENCE_ONLY','QUEUED','FAILED')", name="ck_investigation_status"),
        sa.CheckConstraint("member_total >= member_detailed AND member_detailed >= 0", name="ck_investigation_member_coverage"),
        sa.CheckConstraint("query_planned BETWEEN 0 AND 6", name="ck_investigation_query_budget"),
    )
    op.create_index("ix_investigation_occurrence_created", "investigation", ["occurrence_id", "created_at", "id"])
    op.create_index(
        "uq_investigation_one_active",
        "investigation",
        ["occurrence_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PREPARING','QUEUED')"),
    )
    op.create_table(
        "initial_investigation_snapshot",
        sa.Column("investigation_id", sa.String(length=36), sa.ForeignKey("investigation.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "investigation_phase_transition_v1",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("investigation_id", sa.String(length=36), sa.ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_phase", sa.String(length=48), nullable=True),
        sa.Column("to_phase", sa.String(length=48), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("investigation_id", "sequence", name="uq_investigation_phase_sequence"),
    )
    op.create_table(
        "evidence_brief_v1",
        sa.Column("investigation_id", sa.String(length=36), sa.ForeignKey("investigation.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("member_total", sa.Integer(), nullable=False),
        sa.Column("member_detailed", sa.Integer(), nullable=False),
        sa.Column("query_planned", sa.Integer(), nullable=False),
        sa.Column("query_completed", sa.Integer(), nullable=False),
        sa.Column("successful_metric_facts", sa.Integer(), nullable=False),
        sa.Column("empty_metric_facts", sa.Integer(), nullable=False),
        sa.Column("degraded_domains_json", sa.Text(), nullable=False),
        sa.Column("findings_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "investigation_trajectory_head",
        sa.Column("investigation_id", sa.String(length=36), sa.ForeignKey("investigation.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("schema_revision", sa.Integer(), nullable=False),
        sa.Column("latest_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "metric_observation_v1",
        sa.Column("evidence_ref", sa.String(length=32), primary_key=True),
        sa.Column("investigation_id", sa.String(length=36), sa.ForeignKey("investigation.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("alert_ref", sa.String(length=32), nullable=False),
        sa.Column("metric_name", sa.String(length=256), nullable=False),
        sa.Column("l1_summary_json", sa.Text(), nullable=False),
        sa.Column("l2_sample_json", sa.Text(), nullable=False),
        sa.Column("l3_series_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "metric_empty_observation_v1",
        sa.Column("evidence_ref", sa.String(length=32), primary_key=True),
        sa.Column("investigation_id", sa.String(length=36), sa.ForeignKey("investigation.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("alert_ref", sa.String(length=32), nullable=False),
        sa.Column("metric_name", sa.String(length=256), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "investigation_degradation_v1",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("investigation_id", sa.String(length=36), sa.ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("message", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for table in (
        "initial_investigation_snapshot",
        "investigation_phase_transition_v1",
        "evidence_brief_v1",
        "metric_observation_v1",
        "metric_empty_observation_v1",
        "investigation_degradation_v1",
    ):
        _append_only(table)


def downgrade() -> None:
    for table in (
        "investigation_degradation_v1",
        "metric_empty_observation_v1",
        "metric_observation_v1",
        "evidence_brief_v1",
        "investigation_trajectory_head",
        "investigation_phase_transition_v1",
        "initial_investigation_snapshot",
    ):
        op.drop_table(table)
    op.drop_index("uq_investigation_one_active", table_name="investigation")
    op.drop_index("ix_investigation_occurrence_created", table_name="investigation")
    op.drop_table("investigation")
    op.drop_table("investigation_retention_gate")
