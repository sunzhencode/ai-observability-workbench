"""Source isolation regression tests. Visibility comes from the registry (F21)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api import aggregation_rules, alerts, health, incidents
from app.config import settings
from app.db import get_session
from app.models import Alert, Incident
from app.services.aggregation_rules import preview_aggregation_rule
from app.services.ingest import ingest_alerts
from app.services.source_identity import source_id_for_alertmanager


def _alert(
    fingerprint: str,
    alertname: str,
    cluster: str,
    *,
    severity: str = "warning",
) -> dict:
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": alertname,
            "severity": severity,
            "cluster": cluster,
        },
        "annotations": {},
        "startsAt": "2026-07-18T00:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
    }


def _watchdog(fingerprint: str, cluster: str) -> dict:
    return _alert(fingerprint, "Watchdog", cluster, severity="none")


class _NoThanos:
    configured = False



def _register(session, source_id: str, name: str) -> None:
    """Put a source in the registry; F21 makes this the only way to be visible."""
    from app.registry_models import EventSource, F20Model

    F20Model.metadata.create_all(session.get_bind())
    now = datetime.now(timezone.utc)
    session.add(
        EventSource(
            id=source_id,
            type="ALERTMANAGER",
            name=name,
            lifecycle_state="ENABLED",
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    session.commit()


def _set_state(session, source_id: str, state: str) -> None:
    from app.registry_models import EventSource

    source = session.get(EventSource, source_id)
    source.lifecycle_state = state
    session.add(source)
    session.commit()


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(aggregation_rules.router, prefix="/api")
    app.include_router(alerts.router, prefix="/api")
    app.include_router(health.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[aggregation_rules.get_thanos_client] = lambda: _NoThanos()
    return TestClient(app)


def test_source_id_is_stable_for_equivalent_base_urls() -> None:
    first = source_id_for_alertmanager(" HTTP://Alertmanager.Example.COM/root/ ")
    second = source_id_for_alertmanager("http://alertmanager.example.com/root")

    assert first == second
    assert first.startswith("am:")
    assert "example" not in first
    assert first != source_id_for_alertmanager("http://alertmanager.example.com/other")


def test_same_upstream_fingerprint_is_isolated_between_sources(session) -> None:
    source_a = source_id_for_alertmanager("http://alertmanager-a.test")
    source_b = source_id_for_alertmanager("http://alertmanager-b.test")
    now = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
    raw = [_alert("same-upstream-fp", "TargetDown", "cluster-a")]

    ingest_alerts(session, raw, poll_time=now, source_id=source_a)
    ingest_alerts(session, raw, poll_time=now, source_id=source_b)

    stored = session.exec(select(Alert).order_by(Alert.source_id)).all()
    assert len(stored) == 2
    assert {item.source_id for item in stored} == {source_a, source_b}
    assert {item.upstream_fingerprint for item in stored} == {"same-upstream-fp"}
    assert len({item.fingerprint for item in stored}) == 2
    assert all(item.source_state == "firing" for item in stored)

    grouped = session.exec(select(Incident).order_by(Incident.source_id)).all()
    assert len(grouped) == 2
    assert {item.source_id for item in grouped} == {source_a, source_b}
    assert len({item.group_key for item in grouped}) == 2
    assert all("same-upstream-fp" in item.grouping_explanation for item in grouped)
    assert all(source_a not in item.grouping_explanation for item in grouped)
    assert all(source_b not in item.grouping_explanation for item in grouped)


def test_lifecycle_only_changes_the_polled_source(session) -> None:
    source_a = source_id_for_alertmanager("http://alertmanager-a.test")
    source_b = source_id_for_alertmanager("http://alertmanager-b.test")
    t0 = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
    raw = [_alert("same-upstream-fp", "TargetDown", "cluster-a")]
    ingest_alerts(session, raw, poll_time=t0, source_id=source_a)
    ingest_alerts(session, raw, poll_time=t0, source_id=source_b)

    ingest_alerts(
        session,
        [],
        poll_time=t0 + timedelta(minutes=1),
        resolution_grace_seconds=300,
        source_id=source_a,
    )
    by_source = {
        item.source_id: item for item in session.exec(select(Alert)).all()
    }
    assert by_source[source_a].source_state == "pending_resolution"
    assert by_source[source_b].source_state == "firing"

    ingest_alerts(
        session,
        [],
        poll_time=t0 + timedelta(minutes=10),
        resolution_grace_seconds=300,
        source_id=source_a,
    )
    assert by_source[source_a].source_state == "resolved"
    assert by_source[source_b].source_state == "firing"


def test_registry_decides_visibility_not_the_environment(session, monkeypatch) -> None:
    """F21 replaced `.env` switching with the registry.

    Two enabled sources are both visible at once -- that is F20's global view.
    Narrowing happens explicitly via ?source_ids=, and disabling a source is
    what actually hides its rows. Switching `.env` must change nothing.
    """
    url_a = "http://alertmanager-a.test"
    url_b = "http://alertmanager-b.test"
    source_a = source_id_for_alertmanager(url_a)
    source_b = source_id_for_alertmanager(url_b)
    now = datetime.now(timezone.utc)

    ingest_alerts(
        session,
        [_alert("shared-fp", "AlertFromA", "cluster-a")],
        poll_time=now,
        source_id=source_a,
    )
    ingest_alerts(
        session,
        [_alert("shared-fp", "AlertFromB", "cluster-b")],
        poll_time=now,
        source_id=source_b,
    )
    _register(session, source_a, "Source A")
    _register(session, source_b, "Source B")
    client = _client(session)

    # Both enabled: the global view shows both, regardless of `.env`.
    monkeypatch.setattr(settings, "alertmanager_url", url_a)
    titles = sorted(item["title"] for item in client.get("/api/incidents").json())
    assert titles == ["AlertFromA", "AlertFromB"]
    monkeypatch.setattr(settings, "alertmanager_url", url_b)
    assert sorted(
        item["title"] for item in client.get("/api/incidents").json()
    ) == titles

    # Explicit narrowing is the supported way to see one source.
    only_a = client.get(f"/api/incidents?source_ids={source_a}").json()
    assert [item["title"] for item in only_a] == ["AlertFromA"]

    # Per-source detail stays reachable while the source is enabled.
    incidents_by_source = {
        item.source_id: item for item in session.exec(select(Incident)).all()
    }
    assert (
        client.get(f"/api/incidents/{incidents_by_source[source_b].id}").status_code
        == 200
    )

    # Disabling removes a source from the global list...
    _set_state(session, source_b, "DISABLED")
    assert [item["title"] for item in client.get("/api/incidents").json()] == [
        "AlertFromA"
    ]

    # ...but a directly requested source stays reachable, because F20 keeps a
    # disabled source's history and marks it STALE rather than erasing it.
    assert (
        client.get(f"/api/incidents/{incidents_by_source[source_b].id}").status_code
        == 200
    )
    alerts_by_source = {
        item.source_id: item for item in session.exec(select(Alert)).all()
    }
    for source_id in (source_a, source_b):
        assert (
            client.get(f"/api/alerts/{alerts_by_source[source_id].id}").status_code
            == 200
        )

    # An id that belongs to no registered source is not reachable at all.
    _unregistered = session.exec(select(Incident)).first()
    assert client.get("/api/incidents/99999").status_code == 404

    # Label catalog and preview follow the same visibility rule.
    catalog = client.get("/api/aggregation-labels?lookback_hours=168").json()
    alertname = next(item for item in catalog["labels"] if item["name"] == "alertname")
    assert alertname["sample_values"] == ["AlertFromA"]

    preview = preview_aggregation_rule(
        session,
        rule_id=None,
        name="all visible alerts",
        priority=10,
        enabled=True,
        matchers=[],
        group_by_labels=["cluster"],
        source_id=source_a,
    )
    assert preview.matcher_alert_count == 1
