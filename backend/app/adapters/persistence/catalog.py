"""SQLAlchemy adapter for Service Catalog and deterministic Service Mapping."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, cast

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.adapters.persistence.incidents import (
    IncidentTimelineEntryRecord,
    OperationalOccurrenceRecord,
)
from app.adapters.persistence.jobs import JobRepository
from app.adapters.persistence.sources import AlertRecord
from app.application.catalog import (
    ServiceAssignmentProjection,
    ServiceAuditView,
    ServiceMappingPreview,
    ServiceMappingPreviewSample,
    ServiceMappingPublishResult,
    ServiceMappingRuleView,
    ServiceView,
)
from app.domains.alerting.models import Matcher, MatcherOperator
from app.platform.persistence.codecs import (
    aware_utc as _aware,
    canonical_json as _json,
    stored_utc as _stored,
)
from app.domains.catalog.models import (
    PublishedServiceMappingRule,
    ServiceAssignmentState,
    ServiceCriticality,
    ServiceDraft,
    ServiceMappingMember,
    ServiceMappingRuleDraft,
    ServiceStatus,
    choose_service_assignment,
)
from app.domains.incidents.response import ResponseState
from app.domains.operations.jobs import JobPool, JobSpec
from app.platform.persistence.database import SessionFactory

UTC = timezone.utc
UNMAPPED_ACK_SLA_SECONDS = 15 * 60


class Base(DeclarativeBase):
    pass


class ServiceRecord(Base):
    __tablename__ = "service"
    __table_args__ = (Index("ix_service_status_name", "status", "name", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    criticality: Mapped[str] = mapped_column(String(16), nullable=False)
    ack_sla_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    links_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ServiceAuditRecord(Base):
    __tablename__ = "service_audit"
    __table_args__ = (
        Index("ix_service_audit_service_changed", "service_id", "changed_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("service.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    service_version: Mapped[int] = mapped_column(Integer, nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ServiceMappingRuleRecord(Base):
    __tablename__ = "service_mapping_rule"
    __table_args__ = (Index("ix_service_mapping_rule_order", "priority", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("service.id", ondelete="RESTRICT"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    matchers_json: Mapped[str] = mapped_column(Text, nullable=False)
    published_config_json: Mapped[str | None] = mapped_column(Text)
    published_version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _strings(value: str) -> tuple[str, ...]:
    raw: Any = json.loads(value)
    if not isinstance(raw, list):
        raise RuntimeError("SERVICE_CATALOG_JSON_INVALID")
    return tuple(str(item) for item in raw)


def _matcher_tuples(value: str) -> tuple[tuple[str, str, str], ...]:
    raw: Any = json.loads(value)
    if not isinstance(raw, list):
        raise RuntimeError("SERVICE_MAPPING_JSON_INVALID")
    result: list[tuple[str, str, str]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 3:
            raise RuntimeError("SERVICE_MAPPING_JSON_INVALID")
        result.append((str(item[0]), str(item[1]), str(item[2])))
    return tuple(result)


def _draft_config(record: ServiceMappingRuleRecord) -> str:
    return _json(
        {
            "enabled": record.enabled,
            "matchers": [list(item) for item in _matcher_tuples(record.matchers_json)],
            "priority": record.priority,
            "service_id": record.service_id,
            "source_ids": list(_strings(record.source_ids_json)),
        }
    )


class SqlAlchemyServiceCatalogStore:
    def __init__(
        self,
        sessions: SessionFactory,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self._sessions = sessions
        self._jobs = jobs or JobRepository()

    @staticmethod
    def _service_view(item: ServiceRecord) -> ServiceView:
        created = _aware(item.created_at)
        updated = _aware(item.updated_at)
        if created is None or updated is None:
            raise RuntimeError("SERVICE_TIMESTAMP_MISSING")
        return ServiceView(
            id=item.id,
            name=item.name,
            slug=item.slug,
            criticality=item.criticality,
            ack_sla_seconds=item.ack_sla_seconds,
            status=item.status,
            links=_strings(item.links_json),
            version=item.version,
            created_at=created,
            updated_at=updated,
        )

    @staticmethod
    def _audit_view(item: ServiceAuditRecord) -> ServiceAuditView:
        detail: Any = json.loads(item.detail_json)
        changed = _aware(item.changed_at)
        if not isinstance(detail, dict) or changed is None:
            raise RuntimeError("SERVICE_AUDIT_INVALID")
        return ServiceAuditView(
            item.id,
            item.service_id,
            item.action,
            item.service_version,
            cast(dict[str, object], detail),
            changed,
        )

    @staticmethod
    def _append_service_audit(
        session: Session,
        item: ServiceRecord,
        *,
        action: str,
        now: datetime,
    ) -> None:
        session.add(
            ServiceAuditRecord(
                service_id=item.id,
                action=action,
                service_version=item.version,
                detail_json=_json(
                    {
                        "criticality": item.criticality,
                        "slug": item.slug,
                        "status": item.status,
                    }
                ),
                changed_at=_stored(now),
            )
        )

    def list_services(self, *, include_archived: bool = False) -> tuple[ServiceView, ...]:
        with self._sessions() as session:
            statement = select(ServiceRecord)
            if not include_archived:
                statement = statement.where(ServiceRecord.status == ServiceStatus.ACTIVE.value)
            return tuple(
                self._service_view(item)
                for item in session.scalars(statement.order_by(ServiceRecord.name, ServiceRecord.id))
            )

    def create_service(self, draft: ServiceDraft, *, now: datetime) -> ServiceView:
        with self._sessions.begin() as session:
            item = ServiceRecord(
                name=draft.name.strip(),
                slug=draft.slug.strip(),
                criticality=draft.criticality.value,
                ack_sla_seconds=draft.criticality.ack_sla_seconds,
                status=ServiceStatus.ACTIVE.value,
                links_json=_json(list(draft.links)),
                version=1,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            session.add(item)
            session.flush()
            self._append_service_audit(session, item, action="CREATED", now=now)
            return self._service_view(item)

    def update_service(
        self,
        service_id: int,
        draft: ServiceDraft,
        *,
        expected_version: int,
        now: datetime,
    ) -> ServiceView:
        with self._sessions.begin() as session:
            item = session.get(ServiceRecord, service_id)
            if item is None:
                raise LookupError("SERVICE_NOT_FOUND")
            if item.version != expected_version:
                raise FileExistsError("SERVICE_VERSION_CONFLICT")
            if item.status == ServiceStatus.ARCHIVED.value:
                raise ValueError("SERVICE_ARCHIVED_READ_ONLY")
            item.name = draft.name.strip()
            item.slug = draft.slug.strip()
            item.criticality = draft.criticality.value
            item.ack_sla_seconds = draft.criticality.ack_sla_seconds
            item.links_json = _json(list(draft.links))
            item.version += 1
            item.updated_at = _stored(now)
            session.flush()
            self._append_service_audit(session, item, action="UPDATED", now=now)
            return self._service_view(item)

    def archive_service(
        self, service_id: int, *, expected_version: int, now: datetime
    ) -> ServiceView:
        with self._sessions.begin() as session:
            item = session.get(ServiceRecord, service_id)
            if item is None:
                raise LookupError("SERVICE_NOT_FOUND")
            if item.version != expected_version:
                raise FileExistsError("SERVICE_VERSION_CONFLICT")
            if item.status == ServiceStatus.ARCHIVED.value:
                return self._service_view(item)
            item.status = ServiceStatus.ARCHIVED.value
            item.version += 1
            item.updated_at = _stored(now)
            session.flush()
            self._append_service_audit(session, item, action="ARCHIVED", now=now)
            return self._service_view(item)

    def service_audit(self, service_id: int) -> tuple[ServiceAuditView, ...]:
        with self._sessions() as session:
            if session.get(ServiceRecord, service_id) is None:
                raise LookupError("SERVICE_NOT_FOUND")
            return tuple(
                self._audit_view(item)
                for item in session.scalars(
                    select(ServiceAuditRecord)
                    .where(ServiceAuditRecord.service_id == service_id)
                    .order_by(ServiceAuditRecord.id)
                )
            )

    @staticmethod
    def _rule_view(
        item: ServiceMappingRuleRecord, *, service_name: str
    ) -> ServiceMappingRuleView:
        created = _aware(item.created_at)
        updated = _aware(item.updated_at)
        if created is None or updated is None:
            raise RuntimeError("SERVICE_MAPPING_TIMESTAMP_MISSING")
        return ServiceMappingRuleView(
            id=item.id,
            name=item.name,
            priority=item.priority,
            service_id=item.service_id,
            service_name=service_name,
            enabled=item.enabled,
            source_ids=_strings(item.source_ids_json),
            matchers=_matcher_tuples(item.matchers_json),
            version=item.version,
            published_version=item.published_version,
            published_at=_aware(item.published_at),
            has_unpublished_changes=item.published_config_json != _draft_config(item),
            created_at=created,
            updated_at=updated,
        )

    def _rule_view_in_session(
        self, session: Session, item: ServiceMappingRuleRecord
    ) -> ServiceMappingRuleView:
        service = session.get(ServiceRecord, item.service_id)
        return self._rule_view(
            item, service_name=str(item.service_id) if service is None else service.name
        )

    def list_mapping_rules(self) -> tuple[ServiceMappingRuleView, ...]:
        with self._sessions() as session:
            return tuple(
                self._rule_view_in_session(session, item)
                for item in session.scalars(
                    select(ServiceMappingRuleRecord).order_by(
                        ServiceMappingRuleRecord.priority, ServiceMappingRuleRecord.id
                    )
                )
            )

    @staticmethod
    def _require_active_service(session: Session, service_id: int) -> ServiceRecord:
        service = session.get(ServiceRecord, service_id)
        if service is None:
            raise LookupError("SERVICE_NOT_FOUND")
        if service.status != ServiceStatus.ACTIVE.value:
            raise ValueError("SERVICE_MAPPING_TARGET_ARCHIVED")
        return service

    @staticmethod
    def _set_rule_draft(
        item: ServiceMappingRuleRecord, draft: ServiceMappingRuleDraft
    ) -> None:
        item.name = draft.name.strip()
        item.priority = draft.priority
        item.service_id = draft.service_id
        item.enabled = draft.enabled
        item.source_ids_json = _json(list(draft.source_ids))
        item.matchers_json = _json(
            [[matcher.label, matcher.operator.value, matcher.value] for matcher in draft.matchers]
        )

    def create_mapping_rule(
        self, draft: ServiceMappingRuleDraft, *, now: datetime
    ) -> ServiceMappingRuleView:
        with self._sessions.begin() as session:
            self._require_active_service(session, draft.service_id)
            item = ServiceMappingRuleRecord(
                name=draft.name.strip(),
                priority=draft.priority,
                service_id=draft.service_id,
                enabled=draft.enabled,
                source_ids_json=_json(list(draft.source_ids)),
                matchers_json=_json(
                    [[m.label, m.operator.value, m.value] for m in draft.matchers]
                ),
                published_config_json=None,
                published_version=0,
                published_at=None,
                version=1,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            session.add(item)
            session.flush()
            return self._rule_view_in_session(session, item)

    def update_mapping_rule(
        self,
        rule_id: int,
        draft: ServiceMappingRuleDraft,
        *,
        expected_version: int,
        now: datetime,
    ) -> ServiceMappingRuleView:
        with self._sessions.begin() as session:
            self._require_active_service(session, draft.service_id)
            item = session.get(ServiceMappingRuleRecord, rule_id)
            if item is None:
                raise LookupError("SERVICE_MAPPING_RULE_NOT_FOUND")
            if item.version != expected_version:
                raise FileExistsError("SERVICE_MAPPING_RULE_VERSION_CONFLICT")
            self._set_rule_draft(item, draft)
            item.version += 1
            item.updated_at = _stored(now)
            session.flush()
            return self._rule_view_in_session(session, item)

    @staticmethod
    def _published_rule_from_config(
        rule_id: int, config: dict[str, Any]
    ) -> PublishedServiceMappingRule:
        return PublishedServiceMappingRule(
            id=rule_id,
            priority=int(config["priority"]),
            service_id=int(config["service_id"]),
            enabled=bool(config["enabled"]),
            source_ids=tuple(str(item) for item in config["source_ids"]),
            matchers=tuple(
                Matcher(str(item[0]), MatcherOperator(str(item[1])), str(item[2]))
                for item in config["matchers"]
            ),
        )

    def _published_rules(
        self,
        session: Session,
        *,
        replacement: ServiceMappingRuleRecord | None = None,
    ) -> tuple[PublishedServiceMappingRule, ...]:
        active_services = set(
            session.scalars(
                select(ServiceRecord.id).where(ServiceRecord.status == ServiceStatus.ACTIVE.value)
            )
        )
        values: list[PublishedServiceMappingRule] = []
        for item in session.scalars(select(ServiceMappingRuleRecord)):
            if replacement is not None and item.id == replacement.id:
                raw: Any = json.loads(_draft_config(replacement))
            elif item.published_config_json is not None:
                raw = json.loads(item.published_config_json)
            else:
                continue
            if not isinstance(raw, dict) or int(raw.get("service_id", 0)) not in active_services:
                continue
            values.append(self._published_rule_from_config(item.id, raw))
        return tuple(sorted(values, key=lambda rule: (rule.priority, rule.id)))

    @staticmethod
    def _members(session: Session, incident_id: int) -> tuple[ServiceMappingMember, ...]:
        values: list[ServiceMappingMember] = []
        for item in session.scalars(
            select(AlertRecord).where(AlertRecord.incident_id == incident_id).order_by(AlertRecord.id)
        ):
            labels: Any = json.loads(item.labels_json)
            if not isinstance(labels, dict):
                raise RuntimeError("ALERT_LABELS_INVALID")
            values.append(
                ServiceMappingMember(
                    source_id=item.source_id,
                    labels={str(key): str(value) for key, value in labels.items()},
                )
            )
        return tuple(values)

    def resolve_incident_assignment_in_session(
        self, session: Session, incident_id: int
    ) -> ServiceAssignmentProjection:
        decision = choose_service_assignment(
            self._members(session, incident_id), self._published_rules(session)
        )
        if decision.service_id is None:
            return ServiceAssignmentProjection(
                None,
                "MAPPING" if decision.state is ServiceAssignmentState.SERVICE_AMBIGUOUS else "UNMAPPED",
                decision.state.value,
                UNMAPPED_ACK_SLA_SECONDS,
            )
        service = self._require_active_service(session, decision.service_id)
        return ServiceAssignmentProjection(
            service.id, "MAPPING", ServiceAssignmentState.MAPPED.value, service.ack_sla_seconds
        )

    @staticmethod
    def describe_assignment_in_session(
        session: Session, service_id: int | None, assignment_origin: str
    ) -> tuple[str | None, str]:
        if service_id is None:
            return (
                None,
                ServiceAssignmentState.SERVICE_AMBIGUOUS.value
                if assignment_origin == "MAPPING"
                else ServiceAssignmentState.UNMAPPED.value,
            )
        service = session.get(ServiceRecord, service_id)
        if service is None:
            return None, ServiceAssignmentState.SERVICE_ARCHIVED.value
        return (
            service.name,
            ServiceAssignmentState.SERVICE_ARCHIVED.value
            if service.status == ServiceStatus.ARCHIVED.value
            else ServiceAssignmentState.MAPPED.value,
        )

    @staticmethod
    def active_service_in_session(session: Session, service_id: int) -> tuple[str, int] | None:
        service = session.get(ServiceRecord, service_id)
        if service is None or service.status != ServiceStatus.ACTIVE.value:
            return None
        return service.name, service.ack_sla_seconds

    def preview_mapping_rule(self, rule_id: int) -> ServiceMappingPreview:
        with self._sessions() as session:
            candidate = session.get(ServiceMappingRuleRecord, rule_id)
            if candidate is None:
                raise LookupError("SERVICE_MAPPING_RULE_NOT_FOUND")
            self._require_active_service(session, candidate.service_id)
            rules = self._published_rules(session, replacement=candidate)
            candidate_rule = next(item for item in rules if item.id == candidate.id)
            occurrences = tuple(
                session.scalars(
                    select(OperationalOccurrenceRecord)
                    .where(OperationalOccurrenceRecord.response_state != ResponseState.RESOLVED.value)
                    .order_by(OperationalOccurrenceRecord.id)
                )
            )
            matched_alert_count = 0
            mapped = ambiguous = unmapped = 0
            samples: list[ServiceMappingPreviewSample] = []
            for occurrence in occurrences:
                members = self._members(session, occurrence.incident_id)
                matched_alert_count += sum(
                    candidate_rule.matches(source_id=member.source_id, labels=member.labels)
                    for member in members
                )
                decision = choose_service_assignment(members, rules)
                if decision.state is ServiceAssignmentState.MAPPED:
                    mapped += 1
                elif decision.state is ServiceAssignmentState.SERVICE_AMBIGUOUS:
                    ambiguous += 1
                else:
                    unmapped += 1
                if len(samples) < 20:
                    winners = tuple(
                        sorted(
                            {
                                rule.service_id
                                for member in members
                                if (
                                    rule := next(
                                        (
                                            item
                                            for item in rules
                                            if item.matches(source_id=member.source_id, labels=member.labels)
                                        ),
                                        None,
                                    )
                                ) is not None
                            }
                        )
                    )
                    samples.append(
                        ServiceMappingPreviewSample(
                            occurrence.id, occurrence.title, decision.state.value, winners
                        )
                    )
            return ServiceMappingPreview(
                rule_id,
                matched_alert_count,
                mapped,
                ambiguous,
                unmapped,
                tuple(samples),
            )

    def publish_mapping_rule(
        self, rule_id: int, *, expected_version: int, now: datetime
    ) -> ServiceMappingPublishResult:
        with self._sessions.begin() as session:
            item = session.get(ServiceMappingRuleRecord, rule_id)
            if item is None:
                raise LookupError("SERVICE_MAPPING_RULE_NOT_FOUND")
            if item.version != expected_version:
                raise FileExistsError("SERVICE_MAPPING_RULE_VERSION_CONFLICT")
            self._require_active_service(session, item.service_id)
            if item.published_config_json == _draft_config(item):
                raise ValueError("SERVICE_MAPPING_NO_CHANGES")
            item.published_version += 1
            item.published_config_json = _draft_config(item)
            item.published_at = _stored(now)
            item.version += 1
            item.updated_at = _stored(now)
            session.flush()
            job = self._jobs.enqueue(
                session,
                JobSpec(
                    kind="service-mapping.reproject",
                    pool=JobPool.SOURCE,
                    subject_type="service-mapping-rule",
                    subject_id=str(item.id),
                    payload={"published_version": item.published_version},
                    payload_revision=1,
                    idempotency_key=f"service-mapping-reproject:{item.id}:{item.published_version}",
                ),
                now=now,
            )
            return ServiceMappingPublishResult(self._rule_view_in_session(session, item), job)

    @staticmethod
    def _append_projection_timeline(
        session: Session,
        occurrence: OperationalOccurrenceRecord,
        *,
        previous_service_id: int | None,
        assignment_state: str,
        now: datetime,
    ) -> None:
        sequence = int(
            session.scalar(
                select(func.max(IncidentTimelineEntryRecord.sequence)).where(
                    IncidentTimelineEntryRecord.occurrence_id == occurrence.id
                )
            )
            or 0
        ) + 1
        session.add(
            IncidentTimelineEntryRecord(
                occurrence_id=occurrence.id,
                sequence=sequence,
                actor_type="SYSTEM",
                event_type="SERVICE_MAPPING_UPDATED",
                summary="服务映射规则更新了当前事件归属",
                detail_json=_json(
                    {
                        "assignment_state": assignment_state,
                        "previous_service_id": previous_service_id,
                        "service_id": occurrence.service_id,
                    }
                ),
                request_id="service-mapping-reprojection",
                source_ip="local-system",
                created_at=_stored(now),
            )
        )

    def reproject_open_occurrences(self) -> int:
        now = datetime.now(UTC)
        changed = 0
        with self._sessions.begin() as session:
            for occurrence in session.scalars(
                select(OperationalOccurrenceRecord).where(
                    OperationalOccurrenceRecord.response_state != ResponseState.RESOLVED.value,
                    OperationalOccurrenceRecord.assignment_origin != "MANUAL",
                )
            ):
                projection = self.resolve_incident_assignment_in_session(
                    session, occurrence.incident_id
                )
                if (
                    occurrence.service_id == projection.service_id
                    and occurrence.assignment_origin == projection.assignment_origin
                ):
                    continue
                previous_service_id = occurrence.service_id
                occurrence.service_id = projection.service_id
                occurrence.assignment_origin = projection.assignment_origin
                occurrence.version += 1
                occurrence.latest_activity_at = _stored(now)
                self._append_projection_timeline(
                    session,
                    occurrence,
                    previous_service_id=previous_service_id,
                    assignment_state=projection.assignment_state,
                    now=now,
                )
                changed += 1
        return changed
