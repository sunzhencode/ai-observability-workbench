"""Service Catalog persistence, publication and Occurrence projection contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.adapters.persistence.catalog import SqlAlchemyServiceCatalogStore
from app.adapters.persistence.incidents import (
    IncidentTimelineEntryRecord,
    OperationalOccurrenceRecord,
    SqlAlchemyIncidentStore,
)
from app.adapters.persistence.jobs import JobRecord, JobRepository
from app.adapters.persistence.sources import SqlAlchemySourceStore
from app.api.v1.operator_witness import OperatorWitness
from app.application.incidents import IncidentServiceAssignmentCommands
from app.application.sources import EndpointDraft, SourceDraft
from app.domains.alerting.models import Matcher, MatcherOperator
from app.domains.catalog.models import (
    ServiceCriticality,
    ServiceDraft,
    ServiceMappingRuleDraft,
)
from app.domains.incidents.actors import SystemActor
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations
from app.platform.cursor import SignedCursorCodec
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _fixture(tmp_path: Path):
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "service-catalog.db")
    )
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    catalog = SqlAlchemyServiceCatalogStore(sessions, jobs=JobRepository())
    incidents = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=SignedCursorCodec(b"catalog-test-cursor-key-at-least-32-bytes"),
        service_assignment_resolver=catalog.resolve_incident_assignment_in_session,
        service_descriptor=catalog.describe_assignment_in_session,
        active_service_lookup=catalog.active_service_in_session,
    )
    sources = SqlAlchemySourceStore(sessions, incident_reconciler=incidents)
    sources.create_source(
        SourceDraft(
            "src-a",
            "Primary AM",
            (EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return engine, sessions, catalog, incidents, sources


def _alert(fingerprint: str = "api-down", *, alertname: str = "ApiDown"):
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": alertname,
            "severity": "critical",
            "cluster": "cluster-a",
            "service": "checkout",
        },
        "annotations": {"summary": "checkout unavailable"},
        "startsAt": "2026-08-24T00:00:00Z",
    }


def _apply(sources: SqlAlchemySourceStore, alerts: tuple[dict[str, object], ...], now: datetime):
    snapshot = sources.load_snapshot("src-a", expected_version=1)
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", alerts, 1),)
    )
    assert sources.apply_collection(snapshot, outcome, observed_at=now).committed


def _mapping(service_id: int, *, name: str = "checkout mapping") -> ServiceMappingRuleDraft:
    return ServiceMappingRuleDraft(
        name=name,
        priority=10,
        service_id=service_id,
        enabled=True,
        source_ids=("src-a",),
        matchers=(Matcher("service", MatcherOperator.EQUALS, "checkout"),),
    )


def test_service_audit_and_archive_keep_historical_descriptor(tmp_path: Path) -> None:
    engine, sessions, catalog, _incidents, _sources = _fixture(tmp_path)
    now = datetime(2026, 8, 24, 1, tzinfo=UTC)
    service = catalog.create_service(
        ServiceDraft("Checkout API", "checkout-api", ServiceCriticality.TIER_0),
        now=now,
    )
    updated = catalog.update_service(
        service.id,
        ServiceDraft(
            "Checkout API",
            "checkout-api",
            ServiceCriticality.TIER_1,
            ("https://runbooks.invalid/checkout",),
        ),
        expected_version=service.version,
        now=now + timedelta(minutes=1),
    )
    archived = catalog.archive_service(
        service.id,
        expected_version=updated.version,
        now=now + timedelta(minutes=2),
    )

    assert archived.status == "ARCHIVED"
    assert [item.action for item in catalog.service_audit(service.id)] == [
        "CREATED",
        "UPDATED",
        "ARCHIVED",
    ]
    with sessions() as session:
        assert catalog.describe_assignment_in_session(
            session, service.id, "MANUAL"
        ) == ("Checkout API", "SERVICE_ARCHIVED")
    engine.dispose()


def test_publish_is_atomic_and_new_occurrence_freezes_service_sla(tmp_path: Path) -> None:
    engine, sessions, catalog, incidents, sources = _fixture(tmp_path)
    now = datetime(2026, 8, 24, 1, tzinfo=UTC)
    service = catalog.create_service(
        ServiceDraft("Checkout API", "checkout-api", ServiceCriticality.TIER_0),
        now=now,
    )
    rule = catalog.create_mapping_rule(_mapping(service.id), now=now)
    published = catalog.publish_mapping_rule(
        rule.id, expected_version=rule.version, now=now
    )
    assert published.rule.published_version == 1
    assert published.rule.has_unpublished_changes is False
    with pytest.raises(ValueError, match="SERVICE_MAPPING_NO_CHANGES"):
        catalog.publish_mapping_rule(
            rule.id, expected_version=published.rule.version, now=now
        )
    with sessions() as session:
        job = session.scalar(select(JobRecord))
        assert job is not None
        assert job.kind == "service-mapping.reproject"

    detected = now + timedelta(minutes=1)
    _apply(sources, (_alert(),), detected)
    occurrence = incidents.list_operational_occurrences(
        view="ALL",
        source_ids=(),
        signal_states=(),
        cursor=None,
        limit=20,
        now=detected,
    ).items[0]
    assert occurrence.service_id == service.id
    assert occurrence.service_name == "Checkout API"
    assert occurrence.service_assignment_state == "MAPPED"
    assert occurrence.assignment_origin == "MAPPING"
    assert occurrence.ack_sla_seconds == 300
    assert occurrence.ack_sla_due_at == detected + timedelta(minutes=5)
    engine.dispose()


def test_draft_does_not_change_projection_until_publish_and_sla_never_recalculates(
    tmp_path: Path,
) -> None:
    engine, sessions, catalog, _incidents, sources = _fixture(tmp_path)
    now = datetime(2026, 8, 24, 1, tzinfo=UTC)
    fast = catalog.create_service(
        ServiceDraft("Checkout API", "checkout-api", ServiceCriticality.TIER_0),
        now=now,
    )
    slow = catalog.create_service(
        ServiceDraft("Storefront", "storefront", ServiceCriticality.TIER_3),
        now=now,
    )
    rule = catalog.create_mapping_rule(_mapping(fast.id), now=now)
    published = catalog.publish_mapping_rule(rule.id, expected_version=1, now=now)
    detected = now + timedelta(minutes=1)
    _apply(sources, (_alert(),), detected)
    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None
        original_due = occurrence.ack_sla_due_at
        original_sla = occurrence.ack_sla_seconds

    draft = catalog.update_mapping_rule(
        rule.id,
        _mapping(slow.id),
        expected_version=published.rule.version,
        now=now + timedelta(minutes=2),
    )
    assert draft.has_unpublished_changes is True
    assert catalog.reproject_open_occurrences() == 0
    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None and occurrence.service_id == fast.id

    catalog.publish_mapping_rule(
        rule.id,
        expected_version=draft.version,
        now=now + timedelta(minutes=3),
    )
    assert catalog.reproject_open_occurrences() == 1
    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None
        assert occurrence.service_id == slow.id
        assert occurrence.ack_sla_seconds == original_sla == 300
        assert occurrence.ack_sla_due_at == original_due
        timeline = session.scalar(
            select(IncidentTimelineEntryRecord).where(
                IncidentTimelineEntryRecord.event_type == "SERVICE_MAPPING_UPDATED"
            )
        )
        assert timeline is not None
    engine.dispose()


def test_manual_assignment_is_interactive_current_only_and_blocks_reprojection(
    tmp_path: Path,
) -> None:
    engine, sessions, catalog, incidents, sources = _fixture(tmp_path)
    now = datetime(2026, 8, 24, 1, tzinfo=UTC)
    mapped = catalog.create_service(
        ServiceDraft("Checkout API", "checkout-api", ServiceCriticality.TIER_0),
        now=now,
    )
    manual = catalog.create_service(
        ServiceDraft("Storefront", "storefront", ServiceCriticality.TIER_3),
        now=now,
    )
    rule = catalog.create_mapping_rule(_mapping(mapped.id), now=now)
    catalog.publish_mapping_rule(rule.id, expected_version=1, now=now)
    _apply(sources, (_alert(),), now + timedelta(minutes=1))
    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None
        occurrence_id = occurrence.id
        version = occurrence.version
        frozen_sla = occurrence.ack_sla_seconds

    commands = IncidentServiceAssignmentCommands(incidents)
    try:
        commands.assign(
            occurrence_id,
            service_id=manual.id,
            actor=SystemActor("AI"),  # type: ignore[arg-type]
            expected_version=version,
            idempotency_key="manual-service-system-actor",
            request_id="test-system",
            source_ip="127.0.0.1",
            now=now + timedelta(minutes=2),
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("system actors must not assign Services")

    assigned = commands.assign(
        occurrence_id,
        service_id=manual.id,
        actor=OperatorWitness().actor(),
        expected_version=version,
        idempotency_key="manual-service-operator",
        request_id="test-operator",
        source_ip="127.0.0.1",
        now=now + timedelta(minutes=2),
    )
    assert assigned.assignment_origin == "MANUAL"
    assert assigned.timeline.event_type == "SERVICE_ASSIGNMENT_CHANGED"
    assert catalog.reproject_open_occurrences() == 0
    with sessions() as session:
        occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
        assert occurrence is not None
        assert occurrence.service_id == manual.id
        assert occurrence.assignment_origin == "MANUAL"
        assert occurrence.ack_sla_seconds == frozen_sla == 300
    engine.dispose()
