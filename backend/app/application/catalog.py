"""Service Catalog views and use-case ports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domains.catalog.models import ServiceDraft, ServiceMappingRuleDraft
from app.domains.operations.jobs import JobView


@dataclass(frozen=True, slots=True)
class ServiceView:
    id: int
    name: str
    slug: str
    criticality: str
    ack_sla_seconds: int
    status: str
    links: tuple[str, ...]
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceAuditView:
    id: int
    service_id: int
    action: str
    service_version: int
    detail: dict[str, object]
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceMappingRuleView:
    id: int
    name: str
    priority: int
    service_id: int
    service_name: str
    enabled: bool
    source_ids: tuple[str, ...]
    matchers: tuple[tuple[str, str, str], ...]
    version: int
    published_version: int
    published_at: datetime | None
    has_unpublished_changes: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceMappingPreviewSample:
    occurrence_id: int
    title: str
    assignment_state: str
    service_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ServiceMappingPreview:
    rule_id: int
    matched_alert_count: int
    mapped_occurrence_count: int
    ambiguous_occurrence_count: int
    unmapped_occurrence_count: int
    samples: tuple[ServiceMappingPreviewSample, ...]


@dataclass(frozen=True, slots=True)
class ServiceMappingPublishResult:
    rule: ServiceMappingRuleView
    reprojection_job: JobView


@dataclass(frozen=True, slots=True)
class ServiceAssignmentProjection:
    service_id: int | None
    assignment_origin: str
    assignment_state: str
    ack_sla_seconds: int


class ServiceCatalogPort(Protocol):
    def list_services(self, *, include_archived: bool) -> tuple[ServiceView, ...]: ...
    def create_service(self, draft: ServiceDraft, *, now: datetime) -> ServiceView: ...
    def update_service(
        self, service_id: int, draft: ServiceDraft, *, expected_version: int, now: datetime
    ) -> ServiceView: ...
    def archive_service(
        self, service_id: int, *, expected_version: int, now: datetime
    ) -> ServiceView: ...
    def service_audit(self, service_id: int) -> tuple[ServiceAuditView, ...]: ...
    def list_mapping_rules(self) -> tuple[ServiceMappingRuleView, ...]: ...
    def create_mapping_rule(
        self, draft: ServiceMappingRuleDraft, *, now: datetime
    ) -> ServiceMappingRuleView: ...
    def update_mapping_rule(
        self,
        rule_id: int,
        draft: ServiceMappingRuleDraft,
        *,
        expected_version: int,
        now: datetime,
    ) -> ServiceMappingRuleView: ...
    def preview_mapping_rule(self, rule_id: int) -> ServiceMappingPreview: ...
    def publish_mapping_rule(
        self, rule_id: int, *, expected_version: int, now: datetime
    ) -> ServiceMappingPublishResult: ...
    def reproject_open_occurrences(self) -> int: ...
