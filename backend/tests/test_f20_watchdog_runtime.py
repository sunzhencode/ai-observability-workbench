"""F20 source-level Watchdog inventory and health tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import event_sources, health
from app.db import get_session
from app.registry_models import (
    AlertmanagerEndpointRevision,
    EventSource,
    EventSourceRevision,
    F20Model,
    MonitoredCluster,
)
from app.models import Alert, Incident
from app.services.retention import cleanup_expired_data
from app.services.source_polling import (
    EndpointPollOutcome,
    EndpointRuntimeSnapshot,
    PollCoordinator,
)
from app.services.watchdog import (
    add_expected_cluster,
    derive_watchdog_health,
    list_watchdog_clusters,
)


def _watchdog(
    fingerprint: str, cluster: str | None, *, source_id: str
) -> dict[str, Any]:
    labels = {
        "alertname": "Watchdog",
        "severity": "none",
    }
    if cluster is not None:
        labels["cluster"] = cluster
    return {
        "fingerprint": fingerprint,
        "labels": labels,
        "annotations": {"summary": f"watchdog {cluster or 'missing'}"},
        "startsAt": "2026-07-22T00:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
    }


def _target(fingerprint: str) -> dict[str, Any]:
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": "TargetDown",
            "severity": "warning",
            "cluster": "app-a",
        },
        "annotations": {"summary": "target down"},
        "startsAt": "2026-07-22T00:00:00Z",
    }


@pytest.fixture
def f20_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'watchdog-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    return engine


def _create_source(
    session: Session,
    source_id: str,
    *,
    endpoint_count: int = 1,
    enabled: bool = True,
    watchdog_enabled: bool = True,
    missing_after: int = 60,
) -> EventSource:
    source = EventSource(
        id=source_id,
        name=source_id,
        lifecycle_state="ENABLED" if enabled else "DISABLED",
    )
    session.add(source)
    session.flush()
    revision = EventSourceRevision(
        source_id=source_id,
        revision_no=1,
        internal_state="ACTIVE",
        poll_interval_seconds=30,
        resolution_grace_seconds=0,
        max_parallel_endpoints=endpoint_count,
        watchdog_enabled=watchdog_enabled,
        watchdog_alertname="Watchdog",
        watchdog_identity_label="cluster",
        watchdog_missing_after_seconds=missing_after,
        last_test_status="SUCCESS",
    )
    session.add(revision)
    session.flush()
    source.active_revision_id = revision.id
    session.add(source)
    session.flush()
    for position in range(endpoint_count):
        session.add(
            AlertmanagerEndpointRevision(
                source_revision_id=int(revision.id),
                position=position,
                canonical_url=f"https://{source_id}-ep-{position}.invalid",
                enabled=True,
                auth_kind="NONE",
                username="",
            )
        )
    session.commit()
    return source


def _endpoint(position: int, *, source_id: str) -> EndpointRuntimeSnapshot:
    return EndpointRuntimeSnapshot(
        source_id=source_id,
        source_name=source_id,
        source_revision_id=100 + position,
        endpoint_revision_id=10 + position,
        position=position,
        canonical_url=f"https://{source_id}-ep-{position}.invalid",
        auth_kind="NONE",
        username="",
        secret="",
        timeout_seconds=10,
    )


def _success(
    endpoint: EndpointRuntimeSnapshot, alerts: list[dict[str, Any]]
) -> EndpointPollOutcome:
    return EndpointPollOutcome(endpoint=endpoint, status="SUCCESS", alerts=tuple(alerts))


def _fail(endpoint: EndpointRuntimeSnapshot) -> EndpointPollOutcome:
    return EndpointPollOutcome(
        endpoint=endpoint,
        status="NETWORK",
        safe_error_code="ENDPOINT_NETWORK",
    )


@dataclass
class ScriptedReader:
    scripts: dict[str, list[EndpointPollOutcome]]
    calls: list[str] = field(default_factory=list)

    async def fetch(self, endpoint: EndpointRuntimeSnapshot) -> EndpointPollOutcome:
        self.calls.append(endpoint.canonical_url)
        return self.scripts[endpoint.canonical_url].pop(0)


async def _poll(
    engine,
    scripts: dict[str, list[EndpointPollOutcome]],
    source_id: str,
    when: datetime,
) -> None:
    coordinator = PollCoordinator(engine, endpoint_reader=ScriptedReader(scripts))
    outcome = await coordinator.poll_source_id(source_id, now=when)
    assert outcome.committed is True


def _source_item(health: dict[str, Any], source_id: str) -> dict[str, Any]:
    return next(item for item in health["sources"] if item["source_id"] == source_id)


@pytest.mark.asyncio
async def test_watchdog_inventory_states_api_and_retention_do_not_expire_clusters(
    f20_engine,
) -> None:
    source_id = "src_watchdog_inventory"
    seen_at = datetime(2026, 7, 22, 5, 0, tzinfo=timezone.utc)
    with Session(f20_engine) as session:
        _create_source(session, source_id)

    endpoint = _endpoint(0, source_id=source_id)
    await _poll(
        f20_engine,
        {
            "https://src_watchdog_inventory-ep-0.invalid": [
                _success(
                    endpoint,
                    [
                        _watchdog("wd-a", "cluster-a", source_id=source_id),
                        _watchdog("wd-missing", None, source_id=source_id),
                        _target("target-a"),
                    ],
                )
            ]
        },
        source_id,
        seen_at,
    )

    with Session(f20_engine) as session:
        clusters = list_watchdog_clusters(session, source_id, now=seen_at)
        inventory = [
            (item["identity_value"], item["inventory_state"]) for item in clusters
        ]
        assert inventory == [
            ("<no-cluster>", "DISCOVERED"),
            ("cluster-a", "DISCOVERED"),
        ]
        assert len(session.exec(select(Incident)).all()) == 1
        assert all(
            item.alertname != "Watchdog"
            for item in session.exec(
                select(Alert).where(Alert.incident_id.is_not(None))
            ).all()
        )
        expected = add_expected_cluster(session, source_id, "never-seen", now=seen_at)
        assert expected.inventory_state == "EXPECTED"
        discovered = session.exec(
            select(MonitoredCluster).where(
                MonitoredCluster.source_id == source_id,
                MonitoredCluster.identity_value == "cluster-a",
            )
        ).one()
        from app.services.watchdog import update_cluster_inventory_state

        update_cluster_inventory_state(
            session,
            source_id,
            int(discovered.id),
            action="IGNORE",
            reason="test ignore",
            now=seen_at,
        )
        session.commit()

    await _poll(
        f20_engine,
        {
            "https://src_watchdog_inventory-ep-0.invalid": [
                _success(
                    endpoint,
                    [_watchdog("wd-a", "cluster-a", source_id=source_id)],
                )
            ]
        },
        source_id,
        seen_at + timedelta(minutes=1),
    )

    app = FastAPI()
    app.include_router(event_sources.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: Session(f20_engine)
    api = TestClient(app)
    listed = api.get(f"/api/event-sources/{source_id}/watchdog-clusters").json()
    by_identity = {item["identity_value"]: item for item in listed}
    assert by_identity["cluster-a"]["inventory_state"] == "IGNORED"
    assert by_identity["cluster-a"]["still_emitting"] is True

    patched = api.patch(
        f"/api/event-sources/{source_id}/watchdog-clusters/"
        f"{by_identity['cluster-a']['id']}",
        json={"action": "RESTORE"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["inventory_state"] == "EXPECTED"

    with Session(f20_engine) as session:
        cleanup_expired_data(
            session,
            now=seen_at + timedelta(days=40),
            retention_days=30,
        )
        assert session.exec(select(MonitoredCluster)).all()


@pytest.mark.asyncio
async def test_watchdog_health_complete_partial_failed_and_reenable_grace(
    f20_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = "src_watchdog_health"
    t0 = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)
    with Session(f20_engine) as session:
        _create_source(session, source_id, endpoint_count=2, missing_after=60)
        _create_source(session, "src_disabled", enabled=False, watchdog_enabled=True)
        _create_source(session, "src_unmonitored", watchdog_enabled=False)

    endpoint0 = _endpoint(0, source_id=source_id)
    endpoint1 = _endpoint(1, source_id=source_id)
    scripts = {
        "https://src_watchdog_health-ep-0.invalid": [
            _success(endpoint0, [_watchdog("wd-a", "cluster-a", source_id=source_id)]),
            _success(endpoint0, [_watchdog("wd-a", "cluster-a", source_id=source_id)]),
            _fail(endpoint0),
            _success(endpoint0, []),
            _success(endpoint0, []),
            _success(endpoint0, []),
        ],
        "https://src_watchdog_health-ep-1.invalid": [
            _success(endpoint1, [_watchdog("wd-b", "cluster-b", source_id=source_id)]),
            _fail(endpoint1),
            _fail(endpoint1),
            _success(endpoint1, []),
            _success(endpoint1, []),
            _success(endpoint1, []),
        ],
    }
    coordinator = PollCoordinator(f20_engine, endpoint_reader=ScriptedReader(scripts))

    await coordinator.poll_source_id(source_id, now=t0)
    with Session(f20_engine) as session:
        health_now = derive_watchdog_health(session, now=t0 + timedelta(seconds=1))
        source = _source_item(health_now, source_id)
        assert source["summary"] == {
            "total": 2,
            "healthy": 2,
            "missing": 0,
            "unknown": 0,
        }
        assert health_now["monitored_source_count"] == 1
        assert health_now["unmonitored_source_count"] == 2

    await coordinator.poll_source_id(source_id, now=t0 + timedelta(minutes=1))
    with Session(f20_engine) as session:
        source = _source_item(
            derive_watchdog_health(
                session, now=t0 + timedelta(minutes=1, seconds=1)
            ),
            source_id,
        )
        assert [
            (item["identity_value"], item["health_state"])
            for item in source["clusters"]
        ] == [("cluster-b", "UNKNOWN"), ("cluster-a", "HEALTHY")]

    await coordinator.poll_source_id(source_id, now=t0 + timedelta(minutes=2))
    with Session(f20_engine) as session:
        source = _source_item(
            derive_watchdog_health(
                session, now=t0 + timedelta(minutes=2, seconds=1)
            ),
            source_id,
        )
        assert {item["health_state"] for item in source["clusters"]} == {"UNKNOWN"}

    await coordinator.poll_source_id(source_id, now=t0 + timedelta(minutes=3))
    with Session(f20_engine) as session:
        source = _source_item(
            derive_watchdog_health(
                session, now=t0 + timedelta(minutes=3, seconds=30)
            ),
            source_id,
        )
        assert {item["health_state"] for item in source["clusters"]} == {"HEALTHY"}

    await coordinator.poll_source_id(source_id, now=t0 + timedelta(minutes=4))
    with Session(f20_engine) as session:
        source = _source_item(
            derive_watchdog_health(
                session, now=t0 + timedelta(minutes=4, seconds=1)
            ),
            source_id,
        )
        assert {item["health_state"] for item in source["clusters"]} == {"MISSING"}

    with Session(f20_engine) as session:
        from app.services import event_sources as event_source_service

        source = session.get(EventSource, source_id)
        event_source_service.disable_event_source(
            session, source_id, expected_version=source.version
        )
        session.commit()
        monkeypatch.setattr(
            event_source_service, "_now", lambda: t0 + timedelta(minutes=5)
        )
        source = session.get(EventSource, source_id)
        event_source_service.enable_event_source(
            session, source_id, expected_version=source.version
        )
        session.commit()

    await coordinator.poll_source_id(source_id, now=t0 + timedelta(minutes=5))
    with Session(f20_engine) as session:
        source = _source_item(
            derive_watchdog_health(
                session, now=t0 + timedelta(minutes=5, seconds=30)
            ),
            source_id,
        )
        assert {item["health_state"] for item in source["clusters"]} == {"HEALTHY"}


def test_watchdog_health_api_uses_persisted_inventory_when_event_sources_exist(
    f20_engine,
) -> None:
    source_id = "src_watchdog_api"
    now = datetime.now(timezone.utc)
    with Session(f20_engine) as session:
        source = _create_source(session, source_id)
        add_expected_cluster(session, source.id, "api-cluster", now=now)
        session.commit()

    app = FastAPI()
    app.include_router(health.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: Session(f20_engine)
    body = TestClient(app).get("/api/health").json()

    assert body["watchdog"]["monitored_source_count"] == 1
    assert body["watchdog"]["summary"]["total"] == 1
    assert body["watchdog"]["sources"][0]["source_id"] == source_id


@pytest.mark.asyncio
async def test_back_to_back_polls_do_not_report_a_stale_cluster_as_healthy(
    f20_engine,
) -> None:
    """Round membership must be exact, not "within a second of the run".

    Two polls can land inside the same second -- `poll_source_id` bypasses the
    due filter entirely. A cluster seen only in the first one must be absent
    from the second one's observed set, or it stays HEALTHY forever.
    """
    source_id = "src_same_second"
    t0 = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    with Session(f20_engine) as session:
        _create_source(session, source_id, endpoint_count=1, missing_after=1)

    endpoint = _endpoint(0, source_id=source_id)
    url = f"https://{source_id}-ep-0.invalid"
    scripts = {
        url: [
            _success(endpoint, [_watchdog("wd-a", "cluster-a", source_id=source_id)]),
            _success(endpoint, []),
        ]
    }
    coordinator = PollCoordinator(f20_engine, endpoint_reader=ScriptedReader(scripts))

    await coordinator.poll_source_id(source_id, now=t0)
    with Session(f20_engine) as session:
        source = _source_item(derive_watchdog_health(session, now=t0), source_id)
        assert [item["health_state"] for item in source["clusters"]] == ["HEALTHY"]

    # The next run starts 400ms later and no longer sees the cluster.
    second = t0 + timedelta(milliseconds=400)
    await coordinator.poll_source_id(source_id, now=second)
    with Session(f20_engine) as session:
        source = _source_item(
            derive_watchdog_health(session, now=second + timedelta(seconds=30)),
            source_id,
        )
        assert [item["health_state"] for item in source["clusters"]] == ["MISSING"]
