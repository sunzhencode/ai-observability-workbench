"""Notification enrichment stays local, bounded, and provider-neutral."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import select

from app.adapters.persistence.incidents import OperationalOccurrenceRecord
from app.adapters.persistence.notification_enrichment import (
    read_notification_enrichment_in_session,
)
from app.adapters.persistence.observability import (
    GrafanaTemplateOriginRecord,
    MetricTemplateRecord,
    MonitoringConnectionRecord,
)
from app.adapters.persistence.unified_investigations import (
    EvidenceSnapshotV2Record,
    InvestigationRunV2Record,
    InvestigationToolScopeV2Record,
)
from app.adapters.persistence.sources import IncidentRecord, SqlAlchemySourceStore
from app.application.sources import EndpointDraft, SourceDraft
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _seed_incident(sessions, *, now: datetime) -> int:
    sources = SqlAlchemySourceStore(sessions)
    sources.create_source(
        SourceDraft(
            "source-a",
            "Primary",
            (EndpointDraft(0, "https://alertmanager.invalid"),),
        ),
        now=now,
    )
    sources.seed_alert_for_characterization(
        source_id="source-a",
        raw={
            "fingerprint": "checkout-errors",
            "labels": {
                "alertname": "CheckoutErrorRateHigh",
                "severity": "critical",
                "service": "checkout-api",
            },
            "annotations": {"summary": "Checkout errors"},
            "startsAt": now.isoformat(),
        },
        observed_at=now,
    )
    with sessions() as session:
        incident = session.scalar(select(IncidentRecord))
        assert incident is not None
        return incident.id


def test_reader_projects_existing_l1_evidence_and_grafana_link_without_io(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "e1.db"))
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    observed_at = datetime(2026, 9, 8, 8, 30, tzinfo=UTC)
    stored = observed_at.replace(tzinfo=None)
    incident_id = _seed_incident(sessions, now=observed_at)

    with sessions.begin() as session:
        session.add(
            OperationalOccurrenceRecord(
                id=42,
                incident_id=incident_id,
                occurrence_no=2,
                source_id="source-a",
                group_key="checkout",
                title="Checkout errors",
                signal_state="FIRING",
                signal_severity="critical",
                response_state="UNACKNOWLEDGED",
                response_priority="P1",
                resolution_code=None,
                duplicate_of_occurrence_id=None,
                service_id=None,
                primary_alertname="CheckoutErrorRateHigh",
                aggregation_rule_id=None,
                group_labels_json="{}",
                assignment_origin="UNMAPPED",
                member_count=1,
                evidence_completeness="COMPLETE",
                detected_at=stored,
                source_started_at=stored,
                ack_sla_seconds=900,
                ack_sla_due_at=stored,
                acknowledged_at=None,
                first_investigating_at=None,
                mitigated_at=None,
                resolved_at=None,
                latest_activity_at=stored,
                version=1,
            )
        )
        session.add(
            InvestigationRunV2Record(
                id="run-1",
                occurrence_id=42,
                request_key="start-1",
                provider_profile_id=None,
                model_channel_id=None,
                model_revision=None,
                status="COMPLETED",
                job_id=None,
                request_id="request-1",
                source_ip="127.0.0.1",
                request_count=1,
                tool_call_count=0,
                input_tokens=0,
                output_tokens=0,
                safe_error_code=None,
                created_at=stored,
                updated_at=stored,
            )
        )
        session.flush()
        metrics = [
            {
                "evidence_id": "metric-1",
                "metric_id": "checkout_http_error_ratio",
                "status": "DATA",
                "summary": {"latest": 0.08, "minimum": 0.02, "maximum": 0.08},
                "sample": [[1.0, "0.02"], [2.0, "0.08"]],
            }
        ]
        session.add(
            EvidenceSnapshotV2Record(
                investigation_id="run-1",
                schema_revision=2,
                alert_evidence_json="[]",
                metric_evidence_json=json.dumps(metrics),
                degraded_domains_json="[]",
                content_hash="a" * 64,
                created_at=stored,
            )
        )
        session.add(
            InvestigationToolScopeV2Record(
                investigation_id="run-1",
                source_id="source-a",
                connection_version=1,
                catalog_json=json.dumps(
                    [
                        {
                            "metric_id": "checkout_http_error_ratio",
                            "display_name": "Checkout error ratio",
                            "description": "HTTP error ratio",
                            "unit": "ratio",
                            "expression": "checkout_http_error_ratio",
                        }
                    ]
                ),
                created_at=stored,
            )
        )
        template = MetricTemplateRecord(
            name="Checkout / errors",
            promql="checkout_http_error_ratio",
            description="Grafana: Checkout / Errors",
            required_labels_json="[]",
            legend_format="",
            unit="ratio",
            enabled=True,
            priority=10,
            # Empty is the persisted ALL-sources scope; the Grafana origin still
            # binds this imported panel to source-a for deep-link projection.
            source_ids_json="[]",
            origin_kind="GRAFANA",
            builtin_key=None,
            user_modified=False,
            version=1,
            created_at=stored,
            updated_at=stored,
        )
        session.add(template)
        session.flush()
        session.add(
            GrafanaTemplateOriginRecord(
                template_id=template.id,
                source_id="source-a",
                dashboard_uid="checkout",
                dashboard_title="Checkout API",
                panel_id=7,
                panel_title="Error ratio",
                ref_id="A",
                imported_promql="checkout_http_error_ratio",
                confirmed_promql="checkout_http_error_ratio",
            )
        )
        session.add(
            MonitoringConnectionRecord(
                source_id="source-a",
                kind="GRAFANA",
                base_url="http://grafana.internal:3000",
                secret_envelope=None,
                state="ACTIVE",
                tested_at=stored,
                last_test_code="OK",
                version=1,
                created_at=stored,
                updated_at=stored,
            )
        )

    with sessions() as session:
        result = read_notification_enrichment_in_session(
            session, incident_id, 2, observed_at, True
        )

    assert result.occurrence_id == 42
    assert result.evidence_status == "AVAILABLE"
    assert result.metric_evidence[0].display_name == "Checkout error ratio"
    assert result.metric_evidence[0].latest == 0.08
    assert result.deep_links[0].label == "Checkout API / Error ratio"
    assert result.deep_links[0].url == (
        "http://grafana.internal:3000/d/checkout?"
        "viewPanel=7&from=1788852600000&to=1788856200000"
    )


def test_reader_skips_evidence_for_reminder_but_keeps_occurrence_identity(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "identity.db"))
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    now = datetime(2026, 9, 8, tzinfo=UTC)
    incident_id = _seed_incident(sessions, now=now)
    with sessions.begin() as session:
        session.add(
            OperationalOccurrenceRecord(
                id=9,
                incident_id=incident_id,
                occurrence_no=1,
                source_id="source-a",
                group_key="g",
                title="Incident",
                signal_state="FIRING",
                signal_severity="warning",
                response_state="UNACKNOWLEDGED",
                response_priority="P3",
                resolution_code=None,
                duplicate_of_occurrence_id=None,
                service_id=None,
                primary_alertname=None,
                aggregation_rule_id=None,
                group_labels_json="{}",
                assignment_origin="UNMAPPED",
                member_count=1,
                evidence_completeness="PARTIAL",
                detected_at=now.replace(tzinfo=None),
                source_started_at=now.replace(tzinfo=None),
                ack_sla_seconds=900,
                ack_sla_due_at=None,
                acknowledged_at=None,
                first_investigating_at=None,
                mitigated_at=None,
                resolved_at=None,
                latest_activity_at=now.replace(tzinfo=None),
                version=1,
            )
        )
    with sessions() as session:
        result = read_notification_enrichment_in_session(
            session, incident_id, 1, now, False
        )
    assert result.occurrence_id == 9
    assert result.evidence_status == "NOT_REQUESTED"
    assert result.metric_evidence == ()
    assert result.deep_links == ()
