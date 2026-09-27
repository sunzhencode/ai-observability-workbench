"""Database models (SQLModel) for the alert workbench.

Design notes:
- ``Alert`` stores the latest normalized state per fingerprint. ``first_seen``
  is stable while mutable source fields are refreshed on later polls.
- ``Incident`` is the primary product object: alerts sharing a deterministic
  ``group_key`` are grouped into one incident.
- Lifecycle grace/unknown semantics and handling-state audit are added in later
  phases; MVP derives a simple active/firing view from the latest poll.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, Index, UniqueConstraint, text as sa_text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Alert(SQLModel, table=True):
    """Latest normalized alert state for one source fingerprint."""

    id: Optional[int] = Field(default=None, primary_key=True)
    fingerprint: str = Field(index=True, unique=True)
    upstream_fingerprint: str = Field(default="", index=True)
    source_id: str = Field(default="legacy", index=True)
    environment: str = Field(default="prod", index=True)

    alertname: str = Field(index=True)
    severity: str = Field(default="unknown", index=True)
    cluster: str = Field(default="<no-cluster>", index=True)

    labels: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    annotations: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None

    # MVP: "firing" while present in a poll. Grace/resolved/unknown come later.
    source_state: str = Field(default="firing", index=True)
    missing_since_at: Optional[datetime] = None
    origin: str = Field(default="live")  # live | backfill
    evidence_completeness: str = Field(default="complete")  # complete | reconstructed

    raw_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    incident_id: Optional[int] = Field(default=None, foreign_key="incident.id", index=True)

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)


class Incident(SQLModel, table=True):
    """A deterministic grouping of related alerts."""

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str = Field(default="legacy", index=True)
    environment: str = Field(default="prod", index=True)

    group_key: str = Field(index=True, unique=True)
    title: str = ""
    severity: str = Field(default="unknown", index=True)

    # MVP: derived from member presence in the latest poll (firing | resolved).
    source_state: str = Field(default="firing", index=True)
    handling_state: str = Field(default="NEW", index=True)

    policy_version: int = 1
    grouping_explanation: str = ""
    aggregation_rule_id: Optional[int] = Field(default=None, index=True)
    aggregation_rule_version: Optional[int] = None
    group_labels: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    missing_group_labels: list[str] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    occurrence_no: int = Field(default=1, index=True)
    occurrence_started_at: datetime = Field(default_factory=utcnow)
    change_version: int = Field(default=0)
    change_origin: str = Field(default="SOURCE_ACTIVATION", index=True)
    freshness_state: str = Field(default="FRESH", index=True)
    superseded_by_incident_id: Optional[int] = Field(
        default=None, foreign_key="incident.id", index=True
    )
    group_key_version: int = Field(default=2, index=True)
    legacy_group_key: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class IncidentAudit(SQLModel, table=True):
    """Immutable local audit entry for a handling-state transition."""

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(foreign_key="incident.id", index=True)
    actor: str = "local-user"
    from_state: str
    to_state: str
    reason: str
    created_at: datetime = Field(default_factory=utcnow, index=True)


class GroupingPolicy(SQLModel, table=True):
    """Versioned deterministic grouping definition; exactly one is enabled."""

    id: Optional[int] = Field(default=None, primary_key=True)
    version: int = Field(index=True, unique=True)
    group_by: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class PanelMapping(SQLModel, table=True):
    """One local fallback link for an alertname (scheme B)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    alertname: str = Field(index=True)
    url: str
    label: str
    created_at: datetime = Field(default_factory=utcnow)


class AlertTypeRule(SQLModel, table=True):
    """One version of a deterministic grouping rule for an alertname."""

    id: Optional[int] = Field(default=None, primary_key=True)
    alertname: str = Field(index=True)
    version: int = Field(index=True)
    group_by_labels: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class AggregationRule(SQLModel, table=True):
    """Standalone deterministic alert matching and grouping rule."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    priority: int = Field(default=100, index=True)
    matchers: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    group_by_labels: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    scope_mode: str = Field(default="ALL", index=True)
    enabled: bool = Field(default=True, index=True)
    version: int = Field(default=1)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class GrafanaPanelConfig(SQLModel, table=True):
    """A user-selected Grafana panel and its alert-label variable mappings."""

    id: Optional[int] = Field(default=None, primary_key=True)
    alertname: str = Field(index=True)
    dashboard_uid: str = Field(index=True)
    dashboard_title: str
    panel_id: str
    panel_title: str
    variable_mappings: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, index=True)


class SchemaMigration(SQLModel, table=True):
    """Applied local schema versions and immutable checksums."""

    version: int = Field(primary_key=True)
    name: str
    checksum: str
    applied_at: datetime = Field(default_factory=utcnow)


class ConnectionProfile(SQLModel, table=True):
    """One draft/active/retired revision of a logical source connection."""

    __table_args__ = (
        UniqueConstraint("logical_id", "version", name="uq_connection_revision"),
        Index(
            "uq_connection_logical_draft",
            "logical_id",
            unique=True,
            sqlite_where=sa_text("state = 'DRAFT'"),
        ),
        Index(
            "uq_connection_kind_active",
            "kind",
            unique=True,
            sqlite_where=sa_text("state = 'ACTIVE'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    logical_id: str = Field(index=True)
    version: int
    kind: str = Field(index=True)
    name: str
    state: str = Field(default="DRAFT", index=True)
    base_url: str
    environment: Optional[str] = None
    auth_type: str = "NONE"
    username: str = ""
    secret_envelope: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    settings_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_id: Optional[str] = Field(default=None, index=True)
    last_tested_at: Optional[datetime] = None
    last_test_result: Optional[str] = None
    last_test_error_code: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    activated_at: Optional[datetime] = None


class NotificationChannel(SQLModel, table=True):
    """Stable identity for one Feishu group."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    state: str = Field(default="ENABLED", index=True)
    active_revision_id: Optional[int] = Field(
        default=None, foreign_key="notificationchannelrevision.id"
    )
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    disabled_at: Optional[datetime] = None


class NotificationChannelRevision(SQLModel, table=True):
    """Draft/active/retired channel configuration and encrypted credentials."""

    __table_args__ = (
        UniqueConstraint("channel_id", "version", name="uq_channel_revision"),
        Index(
            "uq_channel_draft",
            "channel_id",
            unique=True,
            sqlite_where=sa_text("state = 'DRAFT'"),
        ),
        Index(
            "uq_channel_active",
            "channel_id",
            unique=True,
            sqlite_where=sa_text("state = 'ACTIVE'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="notificationchannel.id", index=True)
    version: int
    state: str = Field(default="DRAFT", index=True)
    provider: str = Field(default="FEISHU_CUSTOM_BOT")
    webhook_envelope: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    signing_secret_envelope: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON)
    )
    required_keyword: Optional[str] = None
    mention_mode: str = "NONE"
    mention_users: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    mention_on: dict[str, bool] = Field(default_factory=dict, sa_column=Column(JSON))
    # F24: per-provider configuration, secrets included. The five Feishu columns
    # above are kept as additive history -- they are the only record of what an
    # old revision held -- but runtime reads this one. Added by migration v9,
    # which copies existing Feishu revisions in.
    config_envelope: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    last_tested_at: Optional[datetime] = None
    last_test_status: Optional[str] = None
    last_test_error_code: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    activated_at: Optional[datetime] = None


class NotificationPolicyRevision(SQLModel, table=True):
    """Versioned first-match routing policy."""

    __table_args__ = (
        UniqueConstraint("logical_id", "version", name="uq_policy_revision"),
        Index(
            "uq_policy_logical_draft",
            "logical_id",
            unique=True,
            sqlite_where=sa_text("state = 'DRAFT'"),
        ),
        Index(
            "uq_policy_logical_active",
            "logical_id",
            unique=True,
            sqlite_where=sa_text("state = 'ACTIVE'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    logical_id: str = Field(index=True)
    version: int
    name: str
    state: str = Field(default="DRAFT", index=True)
    priority: int = Field(default=100, index=True)
    matchers: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    scope_mode: str = Field(default="ALL", index=True)
    repeat_interval_seconds: int = 14_400
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    activated_at: Optional[datetime] = None


class NotificationPolicyChannel(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "policy_revision_id", "channel_id", name="uq_policy_channel"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    policy_revision_id: int = Field(
        foreign_key="notificationpolicyrevision.id", index=True
    )
    channel_id: int = Field(foreign_key="notificationchannel.id", index=True)
    order: int = 0


class NotificationRoute(SQLModel, table=True):
    """Immutable routing decision for one Incident occurrence."""

    __table_args__ = (
        UniqueConstraint("incident_id", "occurrence_no", name="uq_route_occurrence"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(foreign_key="incident.id", index=True)
    occurrence_no: int
    policy_revision_id: int = Field(
        foreign_key="notificationpolicyrevision.id", index=True
    )
    policy_name: str
    policy_version: int
    policy_priority: int
    match_context_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    repeat_interval_seconds: int
    status: str = Field(default="ACTIVE", index=True)
    last_notified_severity: Optional[str] = None
    last_successful_event_at: Optional[datetime] = None
    next_reminder_at: Optional[datetime] = Field(default=None, index=True)
    repeat_slot: int = 0
    termination_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    closed_at: Optional[datetime] = None


class NotificationRouteTarget(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("route_id", "channel_id", name="uq_route_channel"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    route_id: int = Field(foreign_key="notificationroute.id", index=True)
    channel_id: int = Field(foreign_key="notificationchannel.id", index=True)
    routed_channel_revision_id: int = Field(
        foreign_key="notificationchannelrevision.id"
    )
    channel_name: str
    provider: str
    channel_version: int
    mention_mode: str
    mention_users: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    mention_on: dict[str, bool] = Field(default_factory=dict, sa_column=Column(JSON))
    opened_success_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None


class NotificationDelivery(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_key: str = Field(index=True, unique=True)
    incident_id: int = Field(foreign_key="incident.id", index=True)
    route_id: int = Field(foreign_key="notificationroute.id", index=True)
    route_target_id: int = Field(
        foreign_key="notificationroutetarget.id", index=True
    )
    event_type: str = Field(index=True)
    incident_change_version: int
    repeat_slot: Optional[int] = None
    state: str = Field(default="PENDING", index=True)
    payload_snapshot_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    scheduled_at: datetime = Field(default_factory=utcnow, index=True)
    next_attempt_at: datetime = Field(default_factory=utcnow, index=True)
    attempt_count: int = 0
    next_attempt_trigger: str = "AUTO"
    lease_token: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    suppression_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
    succeeded_at: Optional[datetime] = None


class NotificationAttempt(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("delivery_id", "attempt_no", name="uq_delivery_attempt"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    delivery_id: int = Field(foreign_key="notificationdelivery.id", index=True)
    attempt_no: int
    trigger: str = "AUTO"
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: Optional[datetime] = None
    outcome: Optional[str] = None
    http_status: Optional[int] = None
    provider_request_id: Optional[str] = None
    error_code: Optional[str] = None
    error_summary: Optional[str] = None


class ConfigAudit(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    resource_type: str = Field(index=True)
    resource_id: str = Field(index=True)
    action: str
    actor: str = "local-user"
    redacted_diff_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result: str
    created_at: datetime = Field(default_factory=utcnow, index=True)


class RuntimeSetting(SQLModel, table=True):
    """Non-secret Web-managed runtime flag/value."""

    key: str = Field(primary_key=True)
    value_json: Any = Field(default=None, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow, index=True)
