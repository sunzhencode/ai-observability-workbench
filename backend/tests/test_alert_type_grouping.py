"""Compatibility boundary for the retired per-alertname rule table."""

from __future__ import annotations

from sqlmodel import select

from app.models import AlertTypeRule, Incident
from app.services.ingest import ingest_alerts


def test_legacy_alert_type_rule_is_retained_but_does_not_drive_grouping(session) -> None:
    session.add(
        AlertTypeRule(
            alertname="PodFailure",
            version=1,
            group_by_labels=["cluster", "namespace"],
            enabled=True,
        )
    )
    session.commit()
    ingest_alerts(
        session,
        [
            {
                "fingerprint": "pod-1",
                "labels": {
                    "alertname": "PodFailure",
                    "severity": "warning",
                    "cluster": "prod-a",
                    "namespace": "payments",
                },
                "annotations": {},
                "startsAt": "2026-07-16T09:00:00Z",
            },
            {
                "fingerprint": "pod-2",
                "labels": {
                    "alertname": "PodFailure",
                    "severity": "warning",
                    "cluster": "prod-a",
                    "namespace": "payments",
                },
                "annotations": {},
                "startsAt": "2026-07-16T09:00:00Z",
            },
        ],
    )

    incidents = session.exec(select(Incident).order_by(Incident.group_key)).all()
    assert len(incidents) == 2
    assert all("unmatched_rule" in item.grouping_explanation for item in incidents)
    assert len(session.exec(select(AlertTypeRule)).all()) == 1
