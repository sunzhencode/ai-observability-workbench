"""Add the Service Catalog and published Service Mapping snapshots.

Revision ID: platform_0014
Revises: platform_0013
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0014"
down_revision: str | None = "platform_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("criticality", sa.String(length=16), nullable=False),
        sa.Column("ack_sla_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("links_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_service_name"),
        sa.UniqueConstraint("slug", name="uq_service_slug"),
        sa.CheckConstraint(
            "criticality IN ('TIER_0','TIER_1','TIER_2','TIER_3')",
            name="ck_service_criticality",
        ),
        sa.CheckConstraint(
            "ack_sla_seconds IN (300,900,1800,3600)",
            name="ck_service_ack_sla",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED')", name="ck_service_status"
        ),
        sa.CheckConstraint("version > 0", name="ck_service_version"),
    )
    op.create_index("ix_service_status_name", "service", ["status", "name", "id"])
    op.create_table(
        "service_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("service.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("service_version", sa.Integer(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_service_audit_service_changed",
        "service_audit",
        ["service_id", "changed_at", "id"],
    )
    op.create_table(
        "service_mapping_rule",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("service.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source_ids_json", sa.Text(), nullable=False),
        sa.Column("matchers_json", sa.Text(), nullable=False),
        sa.Column("published_config_json", sa.Text(), nullable=True),
        sa.Column("published_version", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_service_mapping_rule_name"),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 1000000",
            name="ck_service_mapping_rule_priority",
        ),
        sa.CheckConstraint(
            "published_version >= 0", name="ck_service_mapping_published_version"
        ),
        sa.CheckConstraint("version > 0", name="ck_service_mapping_rule_version"),
    )
    op.create_index(
        "ix_service_mapping_rule_order",
        "service_mapping_rule",
        ["priority", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_service_mapping_rule_order", table_name="service_mapping_rule")
    op.drop_table("service_mapping_rule")
    op.drop_index("ix_service_audit_service_changed", table_name="service_audit")
    op.drop_table("service_audit")
    op.drop_index("ix_service_status_name", table_name="service")
    op.drop_table("service")
