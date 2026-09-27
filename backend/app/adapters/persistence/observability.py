"""SQLAlchemy 2 adapter for metrics, Grafana and model channels."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import json
import re
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.adapters.persistence.sources import (
    AggregationRuleRecord,
    AlertRecord,
    IncidentRecord,
    SourceRecord,
)
from app.application.observability import (
    AlertMetricContext,
    GrafanaOriginView,
    GrafanaImportSelection,
    GrafanaImportedState,
    MetricTemplateDraft,
    MetricTemplateView,
    ModelChannelInput,
    ModelChannelSecret,
    ModelChannelView,
    MonitoringConnectionDraft,
    MonitoringConnectionSecret,
    MonitoringConnectionView,
    new_model_channel_id,
    new_prompt_profile_id,
)
from app.platform.persistence.codecs import aware_utc as _aware, stored_utc as _stored
from app.domains.metrics.models import BackfillWindow
from app.domains.investigations.prompt_profiles import (
    BUILTIN_PROFILE_ID,
    PromptGuidanceV1,
    PromptProfileRevisionView,
    PromptProfileView,
    builtin_profile,
    profile_contract_code,
)
from app.domains.investigations.provider_catalog import build_provider_profile
from app.domains.investigations.runtime import (
    ProtocolProfile,
    ProviderId,
    ProviderProfile,
    ProviderSupportLevel,
)
from app.domains.metrics.catalog import BUILTIN_TEMPLATES
from app.domains.metrics.models import placeholder_names
from app.domains.alerting.models import (
    AggregationRule,
    Matcher,
    MatcherOperator,
    choose_aggregation,
    normalize_alert,
)
from app.platform.persistence.database import SessionFactory

def _contains_observed_labels(
    live_labels: Mapping[str, str],
    observed_labels: Mapping[str, str],
) -> bool:
    return bool(observed_labels) and all(
        live_labels.get(key) == value for key, value in observed_labels.items()
    )


class Base(DeclarativeBase):
    pass


class MonitoringConnectionRecord(Base):
    __tablename__ = "monitoring_connection"
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_envelope: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_code: Mapped[str | None] = mapped_column(String(96))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MetricBackfillRunRecord(Base):
    __tablename__ = "metric_backfill_run"
    __table_args__ = (
        Index("ix_metric_backfill_run_completed", "completed_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    requested_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated_reason: Mapped[str | None] = mapped_column(String(96))
    alerts_reconstructed: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MetricTemplateRecord(Base):
    __tablename__ = "metric_template"
    __table_args__ = (Index("ix_metric_template_selection", "enabled", "priority", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    promql: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    required_labels_json: Mapped[str] = mapped_column(Text, nullable=False)
    legend_format: Mapped[str] = mapped_column(String(256), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    origin_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    builtin_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    user_modified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class GrafanaTemplateOriginRecord(Base):
    __tablename__ = "grafana_template_origin"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "dashboard_uid",
            "panel_id",
            "ref_id",
            name="uq_grafana_template_origin_target",
        ),
    )
    template_id: Mapped[int] = mapped_column(Integer, ForeignKey("metric_template.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128))
    dashboard_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    dashboard_title: Mapped[str] = mapped_column(String(256), nullable=False)
    panel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    panel_title: Mapped[str] = mapped_column(String(256), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(32), nullable=False)
    imported_promql: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_promql: Mapped[str] = mapped_column(Text, nullable=False)


class ModelChannelRecord(Base):
    __tablename__ = "model_channel"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    active_revision_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProviderProfileRecord(Base):
    __tablename__ = "provider_profile"
    __table_args__ = (
        UniqueConstraint("provider_id", "revision", name="uq_provider_profile_revision"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    support_level: Mapped[str] = mapped_column(String(24), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ModelChannelRevisionRecord(Base):
    __tablename__ = "model_channel_revision"
    __table_args__ = (
        UniqueConstraint("channel_id", "revision_no", name="uq_model_channel_revision"),
        Index("ix_model_channel_revision_state", "channel_id", "state", "revision_no"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(128), ForeignKey("model_channel.id", ondelete="CASCADE"))
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    api_key_envelope: Mapped[str | None] = mapped_column(Text)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_code: Mapped[str | None] = mapped_column(String(96))
    provider_profile_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("provider_profile.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ModelChannelAuditRecord(Base):
    __tablename__ = "model_channel_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(128), ForeignKey("model_channel.id"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PromptProfileRecord(Base):
    __tablename__ = "prompt_profile"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    active_revision_id: Mapped[int | None] = mapped_column(Integer)
    is_global_default: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PromptProfileRevisionRecord(Base):
    __tablename__ = "prompt_profile_revision"
    __table_args__ = (
        UniqueConstraint("profile_id", "revision_no", name="uq_prompt_profile_revision"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String(128), ForeignKey("prompt_profile.id", ondelete="CASCADE"))
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    guidance_json: Mapped[str] = mapped_column(Text, nullable=False)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_code: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PromptProfileServiceBindingRecord(Base):
    __tablename__ = "prompt_profile_service_binding"
    service_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), ForeignKey("prompt_profile.id", ondelete="CASCADE"))


def _strings(value: str) -> tuple[str, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise RuntimeError("STORED_JSON_INVALID")
    return tuple(str(item) for item in raw)


def _template_fields(draft: MetricTemplateDraft) -> tuple[str, str, str, tuple[str, ...], str, str]:
    name = draft.name.strip()
    promql = draft.promql.strip()
    description = draft.description.strip()
    labels = tuple(dict.fromkeys(item.strip() for item in draft.required_labels if item.strip()))
    if not name or len(name) > 160:
        raise ValueError("METRIC_TEMPLATE_NAME_INVALID")
    if not promql or len(promql) > 4096:
        raise ValueError("METRIC_TEMPLATE_PROMQL_INVALID")
    if len(description) > 2000 or len(labels) > 16:
        raise ValueError("METRIC_TEMPLATE_METADATA_INVALID")
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) is None for item in labels):
        raise ValueError("METRIC_TEMPLATE_LABEL_INVALID")
    if not set(placeholder_names(promql)) <= set(labels):
        raise ValueError("METRIC_TEMPLATE_LABEL_REQUIRED")
    if len(draft.legend_format) > 256 or len(draft.unit) > 64:
        raise ValueError("METRIC_TEMPLATE_METADATA_INVALID")
    return name, promql, description, labels, draft.legend_format.strip(), draft.unit.strip()


def _url(value: str, *, model: bool = False) -> str:
    clean = value.strip().rstrip("/")
    parts = urlsplit(clean)
    allowed = {"https"} if model else {"http", "https"}
    if (
        parts.scheme not in allowed
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or len(clean) > 2048
    ):
        raise ValueError("CONNECTION_URL_INVALID")
    return clean


class SqlAlchemyObservabilityStore:
    def __init__(
        self,
        sessions: SessionFactory,
        *,
        encrypt_secret: Callable[[str], str],
        decrypt_secret: Callable[[str], str],
    ) -> None:
        self._sessions = sessions
        self._encrypt = encrypt_secret
        self._decrypt = decrypt_secret

    def _secret(self, action: str, value: str | None, existing: str | None) -> str | None:
        if action == "KEEP":
            return existing
        if action == "CLEAR":
            return None
        if action != "REPLACE" or not value:
            raise ValueError("SECRET_UPDATE_INVALID")
        return self._encrypt(value)

    @staticmethod
    def _connection_view(row: MonitoringConnectionRecord) -> MonitoringConnectionView:
        return MonitoringConnectionView(
            row.source_id,
            row.kind,
            row.base_url,
            row.state,
            row.secret_envelope is not None,
            _aware(row.tested_at),
            row.last_test_code,
            row.version,
        )

    def save_monitoring_connection(
        self,
        draft: MonitoringConnectionDraft,
        *,
        expected_version: int | None,
        now: datetime,
    ) -> MonitoringConnectionView:
        kind = draft.kind.upper()
        if kind not in {"THANOS", "GRAFANA"}:
            raise ValueError("MONITORING_KIND_INVALID")
        base_url = _url(draft.base_url)
        with self._sessions.begin() as session:
            source = session.get(SourceRecord, draft.source_id)
            if source is None or source.state == "ARCHIVED":
                raise LookupError("SOURCE_NOT_FOUND")
            row = session.get(MonitoringConnectionRecord, (draft.source_id, kind))
            if row is None:
                if expected_version is not None:
                    raise FileExistsError("MONITORING_VERSION_CONFLICT")
                row = MonitoringConnectionRecord(
                    source_id=draft.source_id,
                    kind=kind,
                    base_url=base_url,
                    secret_envelope=self._secret(draft.secret_action, draft.secret_value, None),
                    state="DRAFT",
                    tested_at=None,
                    last_test_code=None,
                    version=1,
                    created_at=_stored(now),
                    updated_at=_stored(now),
                )
                session.add(row)
            else:
                if row.version != expected_version:
                    raise FileExistsError("MONITORING_VERSION_CONFLICT")
                row.base_url = base_url
                row.secret_envelope = self._secret(draft.secret_action, draft.secret_value, row.secret_envelope)
                row.state = "DRAFT"
                row.tested_at = None
                row.last_test_code = None
                row.version += 1
                row.updated_at = _stored(now)
            session.flush()
            return self._connection_view(row)

    def get_monitoring_connection(self, source_id: str, kind: str) -> MonitoringConnectionView | None:
        with self._sessions() as session:
            row = session.get(MonitoringConnectionRecord, (source_id, kind.upper()))
            return None if row is None else self._connection_view(row)

    def list_monitoring_connections(
        self, source_id: str
    ) -> tuple[MonitoringConnectionView, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(MonitoringConnectionRecord)
                .where(MonitoringConnectionRecord.source_id == source_id)
                .order_by(MonitoringConnectionRecord.kind)
            )
            return tuple(self._connection_view(row) for row in rows)

    def list_active_monitoring_connections(self, kind: str) -> tuple[MonitoringConnectionView, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(MonitoringConnectionRecord)
                .join(SourceRecord, SourceRecord.id == MonitoringConnectionRecord.source_id)
                .where(
                    MonitoringConnectionRecord.kind == kind.upper(),
                    MonitoringConnectionRecord.state == "ACTIVE",
                    SourceRecord.state == "ENABLED",
                )
                .order_by(MonitoringConnectionRecord.source_id)
            )
            return tuple(self._connection_view(row) for row in rows)

    def load_monitoring_secret(self, source_id: str, kind: str, *, require_active: bool = False) -> MonitoringConnectionSecret:
        with self._sessions() as session:
            row = session.get(MonitoringConnectionRecord, (source_id, kind.upper()))
            if row is None:
                raise LookupError("MONITORING_CONNECTION_NOT_FOUND")
            if require_active and row.state != "ACTIVE":
                raise RuntimeError("MONITORING_CONNECTION_NOT_ACTIVE")
            secret = "" if row.secret_envelope is None else self._decrypt(row.secret_envelope)
            return MonitoringConnectionSecret(self._connection_view(row), secret)

    def record_monitoring_test(self, source_id: str, kind: str, *, expected_version: int, ok: bool, safe_error_code: str, now: datetime) -> MonitoringConnectionView:
        with self._sessions.begin() as session:
            row = session.get(MonitoringConnectionRecord, (source_id, kind.upper()))
            if row is None:
                raise LookupError("MONITORING_CONNECTION_NOT_FOUND")
            if row.version != expected_version:
                raise FileExistsError("MONITORING_VERSION_CONFLICT")
            row.tested_at = _stored(now)
            row.last_test_code = safe_error_code[:96]
            row.state = "ACTIVE" if ok else "DRAFT"
            row.updated_at = _stored(now)
            session.flush()
            return self._connection_view(row)

    @staticmethod
    def _template_view(session: Session, row: MetricTemplateRecord) -> MetricTemplateView:
        origin = session.get(GrafanaTemplateOriginRecord, row.id)
        grafana_origin = None
        if origin is not None:
            connection = session.get(
                MonitoringConnectionRecord, (origin.source_id, "GRAFANA")
            )
            grafana_origin = GrafanaOriginView(
                origin.source_id,
                origin.dashboard_uid,
                origin.dashboard_title,
                origin.panel_id,
                origin.panel_title,
                "" if connection is None else connection.base_url,
            )
        return MetricTemplateView(
            row.id,
            row.name,
            row.promql,
            row.enabled,
            row.priority,
            _strings(row.source_ids_json),
            row.origin_kind,
            row.version,
            row.description,
            _strings(row.required_labels_json),
            row.legend_format,
            row.unit,
            row.builtin_key,
            row.user_modified,
            grafana_origin,
        )

    def seed_builtin_metric_templates(self, *, now: datetime) -> int:
        """Refresh shipped templates without overwriting operator-owned content."""
        written = 0
        with self._sessions.begin() as session:
            existing = {
                row.builtin_key: row
                for row in session.scalars(
                    select(MetricTemplateRecord).where(
                        MetricTemplateRecord.builtin_key.is_not(None)
                    )
                )
            }
            for builtin in BUILTIN_TEMPLATES:
                row = existing.get(builtin.key)
                labels_json = json.dumps(builtin.required_labels)
                if row is None:
                    session.add(
                        MetricTemplateRecord(
                            name=builtin.name,
                            promql=builtin.promql,
                            description=builtin.description,
                            required_labels_json=labels_json,
                            legend_format="",
                            unit="",
                            enabled=False,
                            priority=100,
                            source_ids_json="[]",
                            origin_kind="MANUAL",
                            builtin_key=builtin.key,
                            user_modified=False,
                            version=1,
                            created_at=_stored(now),
                            updated_at=_stored(now),
                        )
                    )
                    written += 1
                    continue
                if row.user_modified:
                    continue
                if (
                    row.name != builtin.name
                    or row.promql != builtin.promql
                    or row.description != builtin.description
                    or row.required_labels_json != labels_json
                ):
                    row.name = builtin.name
                    row.promql = builtin.promql
                    row.description = builtin.description
                    row.required_labels_json = labels_json
                    row.version += 1
                    row.updated_at = _stored(now)
                    written += 1
        return written

    def list_metric_templates(self) -> tuple[MetricTemplateView, ...]:
        with self._sessions() as session:
            return tuple(
                self._template_view(session, row)
                for row in session.scalars(
                    select(MetricTemplateRecord).order_by(
                        MetricTemplateRecord.priority, MetricTemplateRecord.id
                    )
                )
            )

    def create_metric_template(self, draft: MetricTemplateDraft, *, origin_kind: str, now: datetime) -> MetricTemplateView:
        name, promql, description, labels, legend_format, unit = _template_fields(draft)
        if origin_kind not in {"MANUAL", "GRAFANA"}:
            raise ValueError("METRIC_TEMPLATE_ORIGIN_INVALID")
        with self._sessions.begin() as session:
            for source_id in draft.source_ids:
                if session.get(SourceRecord, source_id) is None:
                    raise ValueError("METRIC_TEMPLATE_SOURCE_UNKNOWN")
            row = MetricTemplateRecord(
                name=name, promql=promql, description=description,
                required_labels_json=json.dumps(labels), legend_format=legend_format, unit=unit,
                enabled=draft.enabled,
                priority=draft.priority, source_ids_json=json.dumps(sorted(set(draft.source_ids))),
                origin_kind=origin_kind, builtin_key=None, user_modified=False,
                version=1, created_at=_stored(now), updated_at=_stored(now),
            )
            session.add(row)
            session.flush()
            return self._template_view(session, row)

    def update_metric_template(self, template_id: int, draft: MetricTemplateDraft, *, expected_version: int, now: datetime) -> MetricTemplateView:
        name, promql, description, labels, legend_format, unit = _template_fields(draft)
        with self._sessions.begin() as session:
            row = session.get(MetricTemplateRecord, template_id)
            if row is None:
                raise LookupError("METRIC_TEMPLATE_NOT_FOUND")
            if row.version != expected_version:
                raise FileExistsError("METRIC_TEMPLATE_VERSION_CONFLICT")
            for source_id in draft.source_ids:
                if session.get(SourceRecord, source_id) is None:
                    raise ValueError("METRIC_TEMPLATE_SOURCE_UNKNOWN")
            content_changed = (
                row.name != name
                or row.promql != promql
                or row.description != description
                or _strings(row.required_labels_json) != labels
            )
            row.name = name
            row.promql = promql
            row.description = description
            row.required_labels_json = json.dumps(labels)
            row.legend_format = legend_format
            row.unit = unit
            row.enabled = draft.enabled
            row.priority = draft.priority
            row.source_ids_json = json.dumps(sorted(set(draft.source_ids)))
            if row.builtin_key is not None and content_changed:
                row.user_modified = True
            row.version += 1
            row.updated_at = _stored(now)
            session.flush()
            return self._template_view(session, row)

    def delete_metric_template(self, template_id: int, *, expected_version: int) -> None:
        with self._sessions.begin() as session:
            row = session.get(MetricTemplateRecord, template_id)
            if row is None:
                raise LookupError("METRIC_TEMPLATE_NOT_FOUND")
            if row.version != expected_version:
                raise FileExistsError("METRIC_TEMPLATE_VERSION_CONFLICT")
            if row.builtin_key is not None:
                raise RuntimeError("METRIC_TEMPLATE_BUILTIN_DELETE_FORBIDDEN")
            session.delete(row)

    def grafana_imported_promql(self, source_id: str, dashboard_uid: str) -> dict[tuple[int, str], str]:
        with self._sessions() as session:
            return {
                (row.panel_id, row.ref_id): row.imported_promql
                for row in session.scalars(
                    select(GrafanaTemplateOriginRecord).where(
                        GrafanaTemplateOriginRecord.source_id == source_id,
                        GrafanaTemplateOriginRecord.dashboard_uid == dashboard_uid,
                    )
                )
            }

    def grafana_import_states(
        self, source_id: str, dashboard_uid: str
    ) -> dict[tuple[int, str], GrafanaImportedState]:
        with self._sessions() as session:
            values: dict[tuple[int, str], GrafanaImportedState] = {}
            rows = session.execute(
                select(GrafanaTemplateOriginRecord, MetricTemplateRecord)
                .join(
                    MetricTemplateRecord,
                    MetricTemplateRecord.id == GrafanaTemplateOriginRecord.template_id,
                )
                .where(
                    GrafanaTemplateOriginRecord.source_id == source_id,
                    GrafanaTemplateOriginRecord.dashboard_uid == dashboard_uid,
                )
            )
            for origin, template in rows:
                values[(origin.panel_id, origin.ref_id)] = GrafanaImportedState(
                    origin.template_id,
                    origin.dashboard_uid,
                    origin.dashboard_title,
                    origin.panel_id,
                    origin.panel_title,
                    origin.ref_id,
                    origin.imported_promql,
                    origin.confirmed_promql,
                    template.promql,
                )
            return values

    def import_grafana_templates(self, source_id: str, selections: tuple[GrafanaImportSelection, ...], *, now: datetime) -> tuple[MetricTemplateView, ...]:
        created: list[MetricTemplateView] = []
        with self._sessions.begin() as session:
            if session.get(SourceRecord, source_id) is None:
                raise LookupError("SOURCE_NOT_FOUND")
            for selection in selections:
                if not selection.final_promql.strip() or len(selection.final_promql) > 4096:
                    raise ValueError("METRIC_TEMPLATE_PROMQL_INVALID")
                candidate = selection.candidate
                origin = session.scalar(
                    select(GrafanaTemplateOriginRecord).where(
                        GrafanaTemplateOriginRecord.source_id == source_id,
                        GrafanaTemplateOriginRecord.dashboard_uid == candidate.dashboard_uid,
                        GrafanaTemplateOriginRecord.panel_id == candidate.panel_id,
                        GrafanaTemplateOriginRecord.ref_id == candidate.ref_id,
                    )
                )
                row = session.get(MetricTemplateRecord, origin.template_id) if origin is not None else None
                if row is None:
                    labels = placeholder_names(selection.final_promql)
                    row = MetricTemplateRecord(
                        name=selection.name.strip(), promql=selection.final_promql.strip(), enabled=True,
                        description=f"Grafana: {candidate.dashboard_title} / {candidate.panel_title}",
                        required_labels_json=json.dumps(labels),
                        legend_format=candidate.legend_format,
                        unit=candidate.unit,
                        priority=selection.priority, source_ids_json=json.dumps([source_id]), origin_kind="GRAFANA",
                        builtin_key=None, user_modified=False,
                        version=1, created_at=_stored(now), updated_at=_stored(now),
                    )
                    session.add(row)
                    session.flush()
                    origin = GrafanaTemplateOriginRecord(
                        template_id=row.id, source_id=source_id, dashboard_uid=candidate.dashboard_uid,
                        dashboard_title=candidate.dashboard_title, panel_id=candidate.panel_id,
                        panel_title=candidate.panel_title, ref_id=candidate.ref_id,
                        imported_promql=candidate.imported_promql,
                        confirmed_promql=selection.final_promql.strip(),
                    )
                    session.add(origin)
                else:
                    labels = placeholder_names(selection.final_promql)
                    row.name = selection.name.strip()
                    row.promql = selection.final_promql.strip()
                    row.description = f"Grafana: {candidate.dashboard_title} / {candidate.panel_title}"
                    row.required_labels_json = json.dumps(labels)
                    row.legend_format = candidate.legend_format
                    row.unit = candidate.unit
                    row.priority = selection.priority
                    row.enabled = True
                    row.version += 1
                    row.updated_at = _stored(now)
                    assert origin is not None
                    origin.dashboard_title = candidate.dashboard_title
                    origin.panel_title = candidate.panel_title
                    origin.imported_promql = candidate.imported_promql
                    origin.confirmed_promql = selection.final_promql.strip()
                session.flush()
                created.append(self._template_view(session, row))
        return tuple(created)

    def get_alert_metric_context(self, alert_id: int) -> AlertMetricContext:
        with self._sessions() as session:
            row = session.get(AlertRecord, alert_id)
            if row is None:
                raise LookupError("ALERT_NOT_FOUND")
            last_seen = _aware(row.last_seen_at)
            labels = json.loads(row.labels_json)
            raw = json.loads(row.raw_json)
            if not isinstance(labels, dict) or not isinstance(raw, dict):
                raise RuntimeError("ALERT_CONTEXT_INVALID")
            generator_url = str(raw.get("generatorURL") or raw.get("generatorUrl") or "")
            return AlertMetricContext(
                row.id,
                row.source_id,
                row.alertname,
                _aware(row.starts_at),
                last_seen,
                {str(key): str(value) for key, value in labels.items()},
                generator_url,
            )

    @staticmethod
    def _aggregation_rules(session: Session) -> tuple[AggregationRule, ...]:
        rules: list[AggregationRule] = []
        for row in session.scalars(
            select(AggregationRuleRecord).order_by(
                AggregationRuleRecord.priority, AggregationRuleRecord.id
            )
        ):
            matchers = json.loads(row.matchers_json)
            rules.append(
                AggregationRule(
                    row.id,
                    row.name,
                    row.priority,
                    row.enabled,
                    tuple(
                        Matcher(str(item[0]), MatcherOperator(str(item[1])), str(item[2]))
                        for item in matchers
                        if isinstance(item, list) and len(item) == 3
                    ),
                    _strings(row.group_by_labels_json),
                    _strings(row.source_ids_json),
                    row.version,
                )
            )
        return tuple(rules)

    def apply_metric_backfill(
        self,
        source_id: str,
        raw_alerts: tuple[dict[str, Any], ...],
        *,
        expected_connection_version: int,
        observed_at: datetime,
        window: BackfillWindow,
    ) -> int:
        with self._sessions.begin() as session:
            connection = session.get(MonitoringConnectionRecord, (source_id, "THANOS"))
            source = session.get(SourceRecord, source_id)
            if (
                connection is None
                or connection.state != "ACTIVE"
                or connection.version != expected_connection_version
                or source is None
                or source.state != "ENABLED"
            ):
                raise RuntimeError("BACKFILL_CONFIG_CHANGED")
            rules = self._aggregation_rules(session)
            active_live_labels_by_alertname: dict[str, list[dict[str, str]]] = {}
            for alertname, labels_json in session.execute(
                select(AlertRecord.alertname, AlertRecord.labels_json).where(
                    AlertRecord.source_id == source_id,
                    AlertRecord.origin == "LIVE_POLL",
                    AlertRecord.source_state != "RECOVERED",
                )
            ):
                active_live_labels_by_alertname.setdefault(alertname, []).append(
                    cast(dict[str, str], json.loads(labels_json))
                )
            accepted_count = 0
            for raw in raw_alerts:
                alert = normalize_alert(raw, source_id=source_id)
                if alert.alertname == source.watchdog_alertname:
                    continue
                if any(
                    _contains_observed_labels(live_labels, alert.labels)
                    for live_labels in active_live_labels_by_alertname.get(
                        alert.alertname, ()
                    )
                ):
                    continue
                existing = session.scalar(
                    select(AlertRecord).where(
                        AlertRecord.source_id == source_id,
                        AlertRecord.upstream_fingerprint == alert.upstream_fingerprint,
                    )
                )
                if existing is not None and existing.origin == "LIVE_POLL":
                    continue
                accepted_count += 1
                end_raw = raw.get("endsAt")
                try:
                    end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                except ValueError:
                    end = observed_at
                decision = choose_aggregation(alert, rules)
                incident = session.scalar(
                    select(IncidentRecord).where(
                        IncidentRecord.source_id == source_id,
                        IncidentRecord.group_key == decision.group_key,
                    )
                )
                if incident is None:
                    incident = IncidentRecord(
                        source_id=source_id,
                        group_key=decision.group_key,
                        title=decision.title,
                        severity=alert.severity,
                        source_state="RECOVERED",
                        freshness_state="HISTORICAL",
                        handling_state="NEW",
                        handling_version=1,
                        aggregation_rule_id=decision.rule_id,
                        aggregation_rule_version=decision.rule_version,
                        group_labels_json=json.dumps(decision.group_labels, sort_keys=True),
                        missing_labels_json=json.dumps(decision.missing_labels),
                        occurrence_no=1,
                        occurrence_started_at=_stored(alert.starts_at or observed_at),
                        change_version=0,
                        change_origin="BACKFILL",
                        updated_at=_stored(end),
                    )
                    session.add(incident)
                    session.flush()
                if existing is None:
                    existing = AlertRecord(
                        source_id=source_id,
                        upstream_fingerprint=alert.upstream_fingerprint,
                        alertname=alert.alertname,
                        severity=alert.severity,
                        cluster=alert.cluster,
                        labels_json=json.dumps(alert.labels, sort_keys=True),
                        annotations_json=json.dumps(alert.annotations, sort_keys=True),
                        raw_json=json.dumps(raw, sort_keys=True),
                        origin="BACKFILL",
                        evidence_completeness="RECONSTRUCTED",
                        source_state="RECOVERED",
                        missing_since_at=None,
                        starts_at=None if alert.starts_at is None else _stored(alert.starts_at),
                        last_seen_at=_stored(end),
                        endpoint_positions_json="[]",
                        incident_id=incident.id,
                    )
                    session.add(existing)
                else:
                    existing.labels_json = json.dumps(alert.labels, sort_keys=True)
                    existing.annotations_json = json.dumps(alert.annotations, sort_keys=True)
                    existing.raw_json = json.dumps(raw, sort_keys=True)
                    existing.evidence_completeness = "RECONSTRUCTED"
                    existing.last_seen_at = _stored(end)
            session.add(
                MetricBackfillRunRecord(
                    source_id=source_id,
                    connection_version=expected_connection_version,
                    window_start=_stored(window.start),
                    window_end=_stored(window.end),
                    requested_hours=window.requested_hours,
                    effective_hours=window.effective_hours,
                    truncated_reason="HARD_LIMIT" if window.truncated else None,
                    alerts_reconstructed=accepted_count,
                    completed_at=_stored(observed_at),
                )
            )
            session.flush()
            return accepted_count

    def _latest_revision(self, session: Session, channel_id: str, *, draft_only: bool = False) -> ModelChannelRevisionRecord:
        statement = select(ModelChannelRevisionRecord).where(ModelChannelRevisionRecord.channel_id == channel_id)
        if draft_only:
            statement = statement.where(ModelChannelRevisionRecord.state == "DRAFT")
        row = session.scalar(statement.order_by(ModelChannelRevisionRecord.revision_no.desc()).limit(1))
        if row is None:
            raise LookupError("MODEL_CHANNEL_REVISION_NOT_FOUND")
        return row

    @staticmethod
    def _model_view(
        session: Session,
        channel: ModelChannelRecord,
        revision: ModelChannelRevisionRecord,
    ) -> ModelChannelView:
        profile = (
            session.get(ProviderProfileRecord, revision.provider_profile_id)
            if revision.provider_profile_id is not None
            else None
        )
        return ModelChannelView(
            channel.id, channel.name, channel.kind, channel.enabled, revision.revision_no,
            revision.state, revision.base_url, revision.model, revision.api_key_envelope is not None,
            _aware(revision.tested_at), revision.last_test_code,
            None if profile is None else profile.id,
            "CUSTOM" if profile is None else profile.provider_id,
            "CHAT_COMPLETIONS" if profile is None else profile.protocol_profile,
            "BEST_EFFORT" if profile is None else profile.support_level,
        )

    @staticmethod
    def _provider_profile(
        session: Session,
        revision: ModelChannelRevisionRecord,
    ) -> ProviderProfile | None:
        if revision.provider_profile_id is None:
            return None
        row = session.get(ProviderProfileRecord, revision.provider_profile_id)
        if row is None:
            raise RuntimeError("MODEL_PROVIDER_PROFILE_NOT_FOUND")
        raw_settings = json.loads(row.settings_json)
        if not isinstance(raw_settings, dict):
            raise RuntimeError("MODEL_PROVIDER_PROFILE_INVALID")
        return ProviderProfile(
            provider_id=ProviderId(row.provider_id),
            protocol=ProtocolProfile(row.protocol_profile),
            support_level=ProviderSupportLevel(row.support_level),
            base_url=row.base_url,
            model_id=revision.model,
            settings=cast(dict[str, object], raw_settings),
        )

    @staticmethod
    def _ensure_provider_profile(
        session: Session,
        draft: ModelChannelInput,
        *,
        base_url: str,
        model: str,
        now: datetime,
    ) -> ProviderProfileRecord:
        try:
            provider_id = ProviderId(draft.provider_id.upper())
        except ValueError as exc:
            raise ValueError("MODEL_PROVIDER_INVALID") from exc
        profile = build_provider_profile(
            provider_id,
            model_id=model,
            custom_base_url=base_url,
        )
        canonical_url = _url(profile.base_url, model=True)
        settings_json = json.dumps(profile.settings or {}, sort_keys=True, separators=(",", ":"))
        existing = session.scalar(
            select(ProviderProfileRecord).where(
                ProviderProfileRecord.provider_id == profile.provider_id.value,
                ProviderProfileRecord.protocol_profile == profile.protocol.value,
                ProviderProfileRecord.support_level == profile.support_level.value,
                ProviderProfileRecord.base_url == canonical_url,
                ProviderProfileRecord.settings_json == settings_json,
            )
        )
        if existing is not None:
            return existing
        latest_revision = session.scalar(
            select(func.max(ProviderProfileRecord.revision)).where(
                ProviderProfileRecord.provider_id == profile.provider_id.value
            )
        )
        provider_revision = int(latest_revision or 0) + 1
        row = ProviderProfileRecord(
            id=f"provider-{uuid4().hex}",
            provider_id=profile.provider_id.value,
            protocol_profile=profile.protocol.value,
            support_level=profile.support_level.value,
            base_url=canonical_url,
            settings_json=settings_json,
            revision=provider_revision,
            created_at=_stored(now),
        )
        session.add(row)
        session.flush()
        return row

    def _activate_model_revision(
        self,
        session: Session,
        channel: ModelChannelRecord,
        revision: ModelChannelRevisionRecord,
        *,
        now: datetime,
        action: str,
    ) -> None:
        for previous in session.scalars(
            select(ModelChannelRevisionRecord).where(
                ModelChannelRevisionRecord.channel_id == channel.id,
                ModelChannelRevisionRecord.id != revision.id,
                ModelChannelRevisionRecord.state != "RETIRED",
            )
        ):
            previous.state = "RETIRED"
        for other in session.scalars(
            select(ModelChannelRecord).where(
                ModelChannelRecord.enabled.is_(True),
                ModelChannelRecord.id != channel.id,
            )
        ):
            other.enabled = False
            other.updated_at = _stored(now)
            other_revision = self._latest_revision(session, other.id)
            session.add(
                ModelChannelAuditRecord(
                    channel_id=other.id,
                    action="DISABLE",
                    revision_no=other_revision.revision_no,
                    changed_at=_stored(now),
                )
            )
        revision.state = "ACTIVE"
        channel.active_revision_id = revision.id
        channel.enabled = True
        channel.updated_at = _stored(now)
        session.add(
            ModelChannelAuditRecord(
                channel_id=channel.id,
                action=action,
                revision_no=revision.revision_no,
                changed_at=_stored(now),
            )
        )

    def create_model_channel(self, draft: ModelChannelInput, *, now: datetime) -> ModelChannelView:
        channel_id = new_model_channel_id()
        if draft.kind != "OPENAI_COMPATIBLE":
            raise ValueError("MODEL_KIND_UNSUPPORTED")
        if not draft.name.strip() or len(draft.name.strip()) > 120:
            raise ValueError("MODEL_CHANNEL_NAME_INVALID")
        if not draft.model.strip():
            raise ValueError("MODEL_NAME_INVALID")
        base_url = _url(draft.base_url, model=True)
        model = draft.model.strip()
        with self._sessions.begin() as session:
            profile = self._ensure_provider_profile(
                session, draft, base_url=base_url, model=model, now=now
            )
            channel = ModelChannelRecord(id=channel_id, name=draft.name.strip(), kind=draft.kind, enabled=False, active_revision_id=None, created_at=_stored(now), updated_at=_stored(now))
            session.add(channel)
            session.flush()
            revision = ModelChannelRevisionRecord(
                channel_id=channel_id, revision_no=1, state="ACTIVE", base_url=profile.base_url,
                model=model, api_key_envelope=self._secret(draft.api_key_action, draft.api_key_value, None),
                tested_at=None, last_test_code=None, provider_profile_id=profile.id,
                created_at=_stored(now),
            )
            session.add(revision)
            session.flush()
            self._activate_model_revision(
                session, channel, revision, now=now, action="CREATE_AND_ACTIVATE"
            )
            session.flush()
            return self._model_view(session, channel, revision)

    def update_model_channel(self, channel_id: str, draft: ModelChannelInput, *, expected_revision: int, now: datetime) -> ModelChannelView:
        with self._sessions.begin() as session:
            channel = session.get(ModelChannelRecord, channel_id)
            if channel is None:
                raise LookupError("MODEL_CHANNEL_NOT_FOUND")
            if draft.kind != channel.kind:
                raise ValueError("MODEL_KIND_IMMUTABLE")
            latest = self._latest_revision(session, channel_id)
            if latest.revision_no != expected_revision:
                raise FileExistsError("MODEL_REVISION_CONFLICT")
            if not draft.model.strip():
                raise ValueError("MODEL_NAME_INVALID")
            base_url = _url(draft.base_url, model=True)
            model = draft.model.strip()
            profile = self._ensure_provider_profile(
                session, draft, base_url=base_url, model=model, now=now
            )
            revision = ModelChannelRevisionRecord(
                channel_id=channel_id, revision_no=expected_revision + 1, state="ACTIVE",
                base_url=profile.base_url, model=model,
                api_key_envelope=self._secret(draft.api_key_action, draft.api_key_value, latest.api_key_envelope),
                tested_at=None, last_test_code=None, provider_profile_id=profile.id,
                created_at=_stored(now),
            )
            channel.name = draft.name.strip()
            channel.updated_at = _stored(now)
            session.add(revision)
            session.flush()
            self._activate_model_revision(
                session, channel, revision, now=now, action="UPDATE_AND_ACTIVATE"
            )
            session.flush()
            return self._model_view(session, channel, revision)

    def load_model_secret(self, channel_id: str, *, require_draft: bool = False) -> ModelChannelSecret:
        with self._sessions() as session:
            channel = session.get(ModelChannelRecord, channel_id)
            if channel is None:
                raise LookupError("MODEL_CHANNEL_NOT_FOUND")
            if require_draft or channel.active_revision_id is None:
                revision = self._latest_revision(session, channel_id, draft_only=require_draft)
            else:
                active_revision = session.get(
                    ModelChannelRevisionRecord, channel.active_revision_id
                )
                if active_revision is None:
                    raise RuntimeError("MODEL_ACTIVE_REVISION_INVALID")
                revision = active_revision
            secret = "" if revision.api_key_envelope is None else self._decrypt(revision.api_key_envelope)
            return ModelChannelSecret(
                self._model_view(session, channel, revision),
                secret,
                self._provider_profile(session, revision),
            )

    def load_model_secret_revision(
        self, channel_id: str, revision_no: int
    ) -> ModelChannelSecret:
        """Load exactly the revision frozen by StartInvestigation."""
        with self._sessions() as session:
            channel = session.get(ModelChannelRecord, channel_id)
            revision = session.scalar(
                select(ModelChannelRevisionRecord).where(
                    ModelChannelRevisionRecord.channel_id == channel_id,
                    ModelChannelRevisionRecord.revision_no == revision_no,
                )
            )
            if channel is None or revision is None:
                raise LookupError("MODEL_CHANNEL_REVISION_NOT_FOUND")
            if (
                not channel.enabled
                or channel.active_revision_id != revision.id
                or revision.state != "ACTIVE"
            ):
                raise RuntimeError("MODEL_CHANNEL_REVISION_NOT_ACTIVE")
            secret = (
                ""
                if revision.api_key_envelope is None
                else self._decrypt(revision.api_key_envelope)
            )
            return ModelChannelSecret(
                self._model_view(session, channel, revision),
                secret,
                self._provider_profile(session, revision),
            )

    def planner_label_keys(self, source_id: str) -> tuple[str, ...]:
        """Derive the Planner allow-set from published source rules/templates."""
        keys: set[str] = set()
        with self._sessions() as session:
            for rule in session.scalars(
                select(AggregationRuleRecord).where(AggregationRuleRecord.enabled.is_(True))
            ):
                source_ids = set(_strings(rule.source_ids_json))
                if source_ids and source_id not in source_ids:
                    continue
                for item in json.loads(rule.matchers_json):
                    if isinstance(item, list) and item:
                        keys.add(str(item[0]))
                keys.update(_strings(rule.group_by_labels_json))
            for template in session.scalars(
                select(MetricTemplateRecord).where(MetricTemplateRecord.enabled.is_(True))
            ):
                source_ids = set(_strings(template.source_ids_json))
                if source_ids and source_id not in source_ids:
                    continue
                keys.update(_strings(template.required_labels_json))
        return tuple(sorted(keys))

    def record_model_test(self, channel_id: str, *, expected_revision: int, ok: bool, safe_error_code: str, now: datetime) -> ModelChannelView:
        with self._sessions.begin() as session:
            channel = session.get(ModelChannelRecord, channel_id)
            if channel is None:
                raise LookupError("MODEL_CHANNEL_NOT_FOUND")
            revision = self._latest_revision(session, channel_id)
            if revision.revision_no != expected_revision:
                raise FileExistsError("MODEL_REVISION_CONFLICT")
            revision.tested_at = _stored(now)
            revision.last_test_code = safe_error_code[:96]
            session.add(ModelChannelAuditRecord(channel_id=channel_id, action="TEST_OK" if ok else "TEST_FAILED", revision_no=revision.revision_no, changed_at=_stored(now)))
            session.flush()
            return self._model_view(session, channel, revision)

    def activate_model_channel(self, channel_id: str, *, expected_revision: int, now: datetime) -> ModelChannelView:
        with self._sessions.begin() as session:
            channel = session.get(ModelChannelRecord, channel_id)
            if channel is None:
                raise LookupError("MODEL_CHANNEL_NOT_FOUND")
            revision = self._latest_revision(session, channel_id)
            if revision.revision_no != expected_revision:
                raise FileExistsError("MODEL_REVISION_CONFLICT")
            self._activate_model_revision(
                session, channel, revision, now=now, action="ACTIVATE"
            )
            session.flush()
            return self._model_view(session, channel, revision)

    def set_model_channel_enabled(self, channel_id: str, *, enabled: bool, now: datetime) -> ModelChannelView:
        with self._sessions.begin() as session:
            channel = session.get(ModelChannelRecord, channel_id)
            if channel is None:
                raise LookupError("MODEL_CHANNEL_NOT_FOUND")
            if enabled and channel.active_revision_id is None:
                raise RuntimeError("MODEL_CHANNEL_NOT_ACTIVE")
            if enabled:
                for other in session.scalars(
                    select(ModelChannelRecord).where(
                        ModelChannelRecord.enabled.is_(True),
                        ModelChannelRecord.id != channel_id,
                    )
                ):
                    other.enabled = False
                    other.updated_at = _stored(now)
            channel.enabled = enabled
            channel.updated_at = _stored(now)
            revision = (
                session.get(ModelChannelRevisionRecord, channel.active_revision_id)
                if channel.active_revision_id is not None
                else None
            )
            if revision is None:
                revision = self._latest_revision(session, channel_id)
            session.add(ModelChannelAuditRecord(channel_id=channel_id, action="ENABLE" if enabled else "DISABLE", revision_no=revision.revision_no, changed_at=_stored(now)))
            session.flush()
            return self._model_view(session, channel, revision)

    def list_model_channels(self) -> tuple[ModelChannelView, ...]:
        with self._sessions() as session:
            views: list[ModelChannelView] = []
            for channel in session.scalars(select(ModelChannelRecord).order_by(ModelChannelRecord.name, ModelChannelRecord.id)):
                views.append(self._model_view(session, channel, self._latest_revision(session, channel.id)))
            return tuple(views)

    def active_model_channel(self) -> ModelChannelView | None:
        with self._sessions() as session:
            channel = session.scalar(
                select(ModelChannelRecord)
                .where(ModelChannelRecord.enabled.is_(True))
                .order_by(ModelChannelRecord.id)
                .limit(1)
            )
            if channel is None or channel.active_revision_id is None:
                return None
            revision = session.get(ModelChannelRevisionRecord, channel.active_revision_id)
            if revision is None or revision.state != "ACTIVE":
                raise RuntimeError("MODEL_ACTIVE_REVISION_INVALID")
            return self._model_view(session, channel, revision)

    @staticmethod
    def _prompt_guidance(revision: PromptProfileRevisionRecord) -> PromptGuidanceV1:
        value = json.loads(revision.guidance_json)
        if not isinstance(value, dict):
            raise RuntimeError("PROMPT_PROFILE_STORED_GUIDANCE_INVALID")
        return PromptGuidanceV1(
            organization_context=str(value.get("organization_context") or ""),
            investigation_focus=str(value.get("investigation_focus") or ""),
            terminology=str(value.get("terminology") or ""),
            response_style=str(value.get("response_style") or ""),
        )

    @staticmethod
    def _prompt_latest(session: Session, profile_id: str) -> PromptProfileRevisionRecord:
        revision = session.scalar(
            select(PromptProfileRevisionRecord)
            .where(PromptProfileRevisionRecord.profile_id == profile_id)
            .order_by(PromptProfileRevisionRecord.revision_no.desc())
            .limit(1)
        )
        if revision is None:
            raise LookupError("PROMPT_PROFILE_REVISION_NOT_FOUND")
        return revision

    def _prompt_view(
        self,
        session: Session,
        profile: PromptProfileRecord,
        revision: PromptProfileRevisionRecord,
    ) -> PromptProfileView:
        service_ids = tuple(
            session.scalars(
                select(PromptProfileServiceBindingRecord.service_id)
                .where(PromptProfileServiceBindingRecord.profile_id == profile.id)
                .order_by(PromptProfileServiceBindingRecord.service_id)
            )
        )
        active_revision = (
            session.get(PromptProfileRevisionRecord, profile.active_revision_id)
            if profile.active_revision_id is not None
            else None
        )
        return PromptProfileView(
            id=profile.id,
            name=profile.name,
            builtin=False,
            status=revision.status,
            revision=revision.revision_no,
            active_revision=(
                active_revision.revision_no if active_revision is not None else None
            ),
            guidance=self._prompt_guidance(revision),
            is_global_default=profile.is_global_default,
            service_ids=service_ids,
            tested_at=_aware(revision.tested_at),
            last_test_code=revision.last_test_code,
        )

    def list_prompt_profiles(self) -> tuple[PromptProfileView, ...]:
        with self._sessions() as session:
            custom = tuple(
                self._prompt_view(session, profile, self._prompt_latest(session, profile.id))
                for profile in session.scalars(
                    select(PromptProfileRecord).order_by(PromptProfileRecord.name, PromptProfileRecord.id)
                )
            )
            has_custom_default = any(
                item.is_global_default and item.active_revision is not None for item in custom
            )
            return (builtin_profile(is_global_default=not has_custom_default), *custom)

    def list_prompt_profile_revisions(
        self, profile_id: str
    ) -> tuple[PromptProfileRevisionView, ...]:
        field_order = (
            "organization_context",
            "investigation_focus",
            "terminology",
            "response_style",
        )
        if profile_id == BUILTIN_PROFILE_ID:
            profile = builtin_profile()
            return (
                PromptProfileRevisionView(
                    revision=profile.revision,
                    status=profile.status,
                    guidance=profile.guidance,
                    tested_at=profile.tested_at,
                    last_test_code=profile.last_test_code,
                    created_at=None,
                    changed_fields=(),
                ),
            )
        with self._sessions() as session:
            if session.get(PromptProfileRecord, profile_id) is None:
                raise LookupError("PROMPT_PROFILE_NOT_FOUND")
            rows = tuple(
                session.scalars(
                    select(PromptProfileRevisionRecord)
                    .where(PromptProfileRevisionRecord.profile_id == profile_id)
                    .order_by(PromptProfileRevisionRecord.revision_no)
                )
            )
            previous = PromptGuidanceV1()
            projected: list[PromptProfileRevisionView] = []
            for row in rows:
                guidance = self._prompt_guidance(row)
                changed = (
                    ()
                    if row.revision_no == 1
                    else tuple(
                        name
                        for name in field_order
                        if getattr(guidance, name) != getattr(previous, name)
                    )
                )
                projected.append(
                    PromptProfileRevisionView(
                        revision=row.revision_no,
                        status=row.status,
                        guidance=guidance,
                        tested_at=_aware(row.tested_at),
                        last_test_code=row.last_test_code,
                        created_at=_aware(row.created_at),
                        changed_fields=changed,
                    )
                )
                previous = guidance
            return tuple(reversed(projected))

    def copy_prompt_profile(self, name: str, *, now: datetime) -> PromptProfileView:
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("PROMPT_PROFILE_NAME_INVALID")
        with self._sessions.begin() as session:
            profile = PromptProfileRecord(
                id=new_prompt_profile_id(),
                name=normalized,
                active_revision_id=None,
                is_global_default=False,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            session.add(profile)
            session.flush()
            revision = PromptProfileRevisionRecord(
                profile_id=profile.id,
                revision_no=1,
                status="DRAFT",
                guidance_json=json.dumps(PromptGuidanceV1().compact(), ensure_ascii=False, sort_keys=True),
                tested_at=None,
                last_test_code=None,
                created_at=_stored(now),
            )
            session.add(revision)
            session.flush()
            return self._prompt_view(session, profile, revision)

    def update_prompt_profile(
        self,
        profile_id: str,
        guidance: PromptGuidanceV1,
        *,
        expected_revision: int,
        now: datetime,
    ) -> PromptProfileView:
        if profile_id == BUILTIN_PROFILE_ID:
            raise PermissionError("BUILTIN_PROMPT_PROFILE_READ_ONLY")
        with self._sessions.begin() as session:
            profile = session.get(PromptProfileRecord, profile_id)
            if profile is None:
                raise LookupError("PROMPT_PROFILE_NOT_FOUND")
            latest = self._prompt_latest(session, profile_id)
            if latest.revision_no != expected_revision:
                raise FileExistsError("PROMPT_PROFILE_REVISION_CONFLICT")
            revision = PromptProfileRevisionRecord(
                profile_id=profile_id,
                revision_no=expected_revision + 1,
                status="DRAFT",
                guidance_json=json.dumps(guidance.compact(), ensure_ascii=False, sort_keys=True),
                tested_at=None,
                last_test_code=None,
                created_at=_stored(now),
            )
            session.add(revision)
            profile.updated_at = _stored(now)
            session.flush()
            return self._prompt_view(session, profile, revision)

    def test_prompt_profile(
        self, profile_id: str, *, expected_revision: int, now: datetime
    ) -> PromptProfileView:
        if profile_id == BUILTIN_PROFILE_ID:
            return builtin_profile()
        with self._sessions.begin() as session:
            profile = session.get(PromptProfileRecord, profile_id)
            if profile is None:
                raise LookupError("PROMPT_PROFILE_NOT_FOUND")
            revision = self._prompt_latest(session, profile_id)
            if revision.revision_no != expected_revision:
                raise FileExistsError("PROMPT_PROFILE_REVISION_CONFLICT")
            view = self._prompt_view(session, profile, revision)
            revision.tested_at = _stored(now)
            revision.last_test_code = profile_contract_code(view)
            session.flush()
            return self._prompt_view(session, profile, revision)

    def activate_prompt_profile(
        self,
        profile_id: str,
        *,
        expected_revision: int,
        global_default: bool,
        service_ids: tuple[int, ...],
        now: datetime,
    ) -> PromptProfileView:
        if profile_id == BUILTIN_PROFILE_ID:
            raise PermissionError("BUILTIN_PROMPT_PROFILE_READ_ONLY")
        with self._sessions.begin() as session:
            profile = session.get(PromptProfileRecord, profile_id)
            if profile is None:
                raise LookupError("PROMPT_PROFILE_NOT_FOUND")
            revision = self._prompt_latest(session, profile_id)
            if revision.revision_no != expected_revision:
                raise FileExistsError("PROMPT_PROFILE_REVISION_CONFLICT")
            for previous in session.scalars(
                select(PromptProfileRevisionRecord).where(
                    PromptProfileRevisionRecord.profile_id == profile_id,
                    PromptProfileRevisionRecord.id != revision.id,
                    PromptProfileRevisionRecord.status != "RETIRED",
                )
            ):
                previous.status = "RETIRED"
            revision.status = "ACTIVE"
            profile.active_revision_id = revision.id
            profile.is_global_default = global_default
            profile.updated_at = _stored(now)
            if global_default:
                for other in session.scalars(
                    select(PromptProfileRecord).where(PromptProfileRecord.id != profile_id)
                ):
                    other.is_global_default = False
            for binding in session.scalars(
                select(PromptProfileServiceBindingRecord).where(
                    PromptProfileServiceBindingRecord.profile_id == profile_id
                )
            ):
                session.delete(binding)
            for service_id in tuple(dict.fromkeys(service_ids)):
                existing = session.get(PromptProfileServiceBindingRecord, service_id)
                if existing is not None:
                    session.delete(existing)
                    session.flush()
                session.add(PromptProfileServiceBindingRecord(service_id=service_id, profile_id=profile_id))
            session.flush()
            return self._prompt_view(session, profile, revision)

    def resolve_prompt_profile(
        self, *, profile_id: str | None, service_id: int | None
    ) -> PromptProfileView:
        if profile_id == BUILTIN_PROFILE_ID:
            return builtin_profile()
        with self._sessions() as session:
            selected_id = profile_id
            if selected_id is None and service_id is not None:
                binding = session.get(PromptProfileServiceBindingRecord, service_id)
                selected_id = None if binding is None else binding.profile_id
            if selected_id is None:
                default = session.scalar(
                    select(PromptProfileRecord)
                    .where(PromptProfileRecord.is_global_default.is_(True))
                    .order_by(PromptProfileRecord.id)
                    .limit(1)
                )
                selected_id = None if default is None else default.id
            if selected_id is None:
                return builtin_profile()
            profile = session.get(PromptProfileRecord, selected_id)
            if profile is None or profile.active_revision_id is None:
                raise LookupError("PROMPT_PROFILE_ACTIVE_REVISION_NOT_FOUND")
            revision = session.get(PromptProfileRevisionRecord, profile.active_revision_id)
            if revision is None or revision.status != "ACTIVE":
                raise RuntimeError("PROMPT_PROFILE_ACTIVE_REVISION_INVALID")
            return self._prompt_view(session, profile, revision)
