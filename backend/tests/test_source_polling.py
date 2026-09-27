"""F20 multi-source polling and HA endpoint merge tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import health
from app.crypto import SecretBox
from app.db import get_session
from app.registry_models import (
    AlertEndpointObservation,
    AlertmanagerEndpointRevision,
    EndpointPollResult,
    EventSource,
    EventSourceRevision,
    F20Model,
    SourcePollRun,
)
from app.models import Alert, Incident, NotificationDelivery, NotificationRouteTarget
from app.providers.feishu import FakeFeishuProvider
from app.services.notification_channels import (
    activate_channel_revision,
    create_channel,
    test_channel_revision,
)
from app.services.notification_policies import activate_policy, create_policy_draft
from app.services.ingest import ingest_alerts
from app.services import source_polling
from app.services.source_polling import (
    EndpointPollOutcome,
    EndpointRuntimeSnapshot,
    PollCoordinator,
    endpoint_identity,
    merge_endpoint_outcomes,
)


def _alert(
    fingerprint: str,
    alertname: str = "TargetDown",
    cluster: str = "cluster-a",
    *,
    starts_at: str = "2026-07-22T00:00:00Z",
    updated_at: str | None = None,
    summary: str = "target down",
) -> dict[str, Any]:
    payload = {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": alertname,
            "severity": "warning",
            "cluster": cluster,
        },
        "annotations": {"summary": summary},
        "startsAt": starts_at,
        "endsAt": "0001-01-01T00:00:00Z",
    }
    if updated_at is not None:
        payload["updatedAt"] = updated_at
    return payload


def _endpoint(position: int, *, source_id: str = "src_a") -> EndpointRuntimeSnapshot:
    return EndpointRuntimeSnapshot(
        source_id=source_id,
        source_name=f"Source {source_id}",
        source_revision_id=100 + position,
        endpoint_revision_id=10 + position,
        position=position,
        canonical_url=f"https://endpoint-{source_id}-{position}.invalid",
        auth_kind="NONE",
        username="",
        secret="",
        timeout_seconds=10.0,
    )


def _success(
    endpoint: EndpointRuntimeSnapshot, alerts: list[dict[str, Any]]
) -> EndpointPollOutcome:
    return EndpointPollOutcome(
        endpoint=endpoint,
        status="SUCCESS",
        alerts=tuple(alerts),
        duration_ms=5,
        safe_error_code=None,
    )


def _failure(endpoint: EndpointRuntimeSnapshot, code: str) -> EndpointPollOutcome:
    return EndpointPollOutcome(
        endpoint=endpoint,
        status="TIMEOUT" if code == "ENDPOINT_TIMEOUT" else "NETWORK",
        alerts=(),
        duration_ms=5,
        safe_error_code=code,
    )


def test_endpoint_union_dedupes_same_fingerprint_across_ha_endpoints() -> None:
    first = _endpoint(0)
    second = _endpoint(1)

    result = merge_endpoint_outcomes(
        [
            _success(first, [_alert("same-fp"), _alert("a-only", cluster="a-only")]),
            _success(second, [_alert("same-fp"), _alert("b-only", cluster="b-only")]),
        ]
    )

    assert result.completeness == "COMPLETE"
    assert [item.identity for item in result.alerts] == [
        "a-only",
        "b-only",
        "same-fp",
    ]
    same = next(item for item in result.alerts if item.identity == "same-fp")
    assert same.endpoint_revision_ids == (10, 11)
    assert result.safe_error_codes == ()


def test_missing_fingerprint_uses_canonical_labels_hash_identity() -> None:
    first = _endpoint(0)
    second = _endpoint(1)
    raw = _alert("", cluster="shared")
    raw.pop("fingerprint")

    result = merge_endpoint_outcomes(
        [
            _success(first, [raw]),
            _success(second, [{**raw, "annotations": {"summary": "different text"}}]),
        ]
    )

    assert len(result.alerts) == 1
    identity = result.alerts[0].identity
    assert identity.startswith("labels:")
    assert identity == endpoint_identity(raw)
    assert result.alerts[0].endpoint_revision_ids == (10, 11)


def test_same_identity_payload_divergence_chooses_newer_then_endpoint_order() -> None:
    first = _endpoint(0)
    second = _endpoint(1)
    older = _alert(
        "same-fp",
        starts_at="2026-07-22T00:00:00Z",
        updated_at="2026-07-22T00:01:00Z",
        summary="older",
    )
    newer = _alert(
        "same-fp",
        starts_at="2026-07-22T00:00:00Z",
        updated_at="2026-07-22T00:02:00Z",
        summary="newer",
    )

    result = merge_endpoint_outcomes([_success(first, [older]), _success(second, [newer])])

    assert result.completeness == "COMPLETE"
    assert result.alerts[0].raw["annotations"]["summary"] == "newer"
    assert result.safe_error_codes == ("PAYLOAD_DIVERGENCE",)


@pytest.mark.parametrize(
    ("outcomes", "expected_completeness", "expected_codes"),
    [
        (
            [_success(_endpoint(0), [_alert("one")]), _success(_endpoint(1), [])],
            "COMPLETE",
            (),
        ),
        (
            [_success(_endpoint(0), [_alert("one")]), _failure(_endpoint(1), "ENDPOINT_TIMEOUT")],
            "PARTIAL",
            ("ENDPOINT_TIMEOUT", "PARTIAL_POLL"),
        ),
        (
            [
                _failure(_endpoint(0), "ENDPOINT_TIMEOUT"),
                _failure(_endpoint(1), "ENDPOINT_NETWORK"),
            ],
            "FAILED",
            ("ALL_ENDPOINTS_FAILED", "ENDPOINT_NETWORK", "ENDPOINT_TIMEOUT"),
        ),
    ],
)
def test_merge_completeness_matrix(outcomes, expected_completeness, expected_codes) -> None:
    result = merge_endpoint_outcomes(outcomes)

    assert result.completeness == expected_completeness
    assert result.safe_error_codes == expected_codes


@pytest.fixture
def f20_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'source-polling.db'}",
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
    resolution_grace_seconds: int = 0,
    poll_interval_seconds: int = 30,
    name: str | None = None,
) -> EventSource:
    source = EventSource(
        id=source_id,
        name=name or source_id,
        lifecycle_state="ENABLED",
    )
    session.add(source)
    session.flush()
    revision = EventSourceRevision(
        source_id=source_id,
        revision_no=1,
        internal_state="ACTIVE",
        poll_interval_seconds=poll_interval_seconds,
        resolution_grace_seconds=resolution_grace_seconds,
        max_parallel_endpoints=endpoint_count,
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


async def _configure_fake_notification_policy(session: Session) -> None:
    box = SecretBox("source-polling-notification-key")
    channel, revision = create_channel(
        session,
        name="source polling channel",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/source-polling",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(
        session,
        int(revision.id),
        box=box,
        provider=FakeFeishuProvider(),
    )
    activate_channel_revision(session, int(revision.id), expected_version=1, box=box)
    policy = create_policy_draft(
        session,
        name="source polling catch all",
        priority=100,
        matchers=[],
        repeat_interval_seconds=14_400,
        channel_ids=[int(channel.id)],
    )
    activate_policy(session, int(policy.id), expected_version=1)
    session.commit()


@dataclass
class ScriptedEndpointReader:
    scripts: dict[str, list[EndpointPollOutcome]]
    delay: float = 0.0
    calls: list[str] = field(default_factory=list)
    active: int = 0
    max_active: int = 0

    async def fetch(self, endpoint: EndpointRuntimeSnapshot) -> EndpointPollOutcome:
        self.calls.append(endpoint.canonical_url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.active -= 1
        values = self.scripts[endpoint.canonical_url]
        outcome = values.pop(0)
        return EndpointPollOutcome(
            endpoint=endpoint,
            status=outcome.status,
            alerts=outcome.alerts,
            duration_ms=outcome.duration_ms,
            safe_error_code=outcome.safe_error_code,
        )


@pytest.mark.asyncio
async def test_coordinator_uses_immutable_source_snapshot(f20_engine) -> None:
    with Session(f20_engine) as session:
        _create_source(session, "src_snapshot")

    first_seen = asyncio.Event()
    release = asyncio.Event()

    class BlockingReader(ScriptedEndpointReader):
        async def fetch(self, endpoint: EndpointRuntimeSnapshot) -> EndpointPollOutcome:
            self.calls.append(endpoint.canonical_url)
            first_seen.set()
            await release.wait()
            return _success(endpoint, [_alert("snapshot-fp")])

    reader = BlockingReader({})
    coordinator = PollCoordinator(f20_engine, endpoint_reader=reader)
    task = asyncio.create_task(
        coordinator.poll_enabled_sources(
            now=datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)
        )
    )
    await asyncio.wait_for(first_seen.wait(), timeout=1)

    with Session(f20_engine) as session:
        revision = session.exec(select(EventSourceRevision)).one()
        endpoint = session.exec(select(AlertmanagerEndpointRevision)).one()
        endpoint.canonical_url = "https://mutated-after-snapshot.invalid"
        session.add(endpoint)
        session.commit()
        old_revision_id = revision.id

    release.set()
    await task

    assert reader.calls == ["https://src_snapshot-ep-0.invalid"]
    with Session(f20_engine) as session:
        run = session.exec(select(SourcePollRun)).one()
        assert run.source_revision_id == old_revision_id


@pytest.mark.asyncio
async def test_coordinator_polls_only_sources_whose_own_interval_is_due(
    f20_engine,
) -> None:
    now = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)
    with Session(f20_engine) as session:
        fast = _create_source(session, "src_fast", poll_interval_seconds=30)
        slow = _create_source(session, "src_slow", poll_interval_seconds=120)
        for source in (fast, slow):
            session.add(
                SourcePollRun(
                    source_id=source.id,
                    source_revision_id=int(source.active_revision_id),
                    started_at=now - timedelta(seconds=60),
                    finished_at=now - timedelta(seconds=60),
                    completeness="COMPLETE",
                )
            )
        session.commit()

    scripts = {
        "https://src_fast-ep-0.invalid": [
            _success(_endpoint(0, source_id="src_fast"), [_alert("fast-fp")])
        ]
    }
    reader = ScriptedEndpointReader(scripts)
    coordinator = PollCoordinator(f20_engine, endpoint_reader=reader)

    outcomes = await coordinator.poll_enabled_sources(now=now)

    assert [item.source_id for item in outcomes] == ["src_fast"]
    assert reader.calls == ["https://src_fast-ep-0.invalid"]
    with Session(f20_engine) as session:
        runs = session.exec(
            select(SourcePollRun).order_by(
                SourcePollRun.source_id, SourcePollRun.started_at
            )
        ).all()
        assert sum(item.source_id == "src_fast" for item in runs) == 2
        assert sum(item.source_id == "src_slow" for item in runs) == 1


@pytest.mark.asyncio
async def test_in_flight_poll_is_discarded_after_source_is_disabled(
    f20_engine,
) -> None:
    from app.services.event_sources import disable_event_source

    with Session(f20_engine) as session:
        _create_source(session, "src_race")

    first_seen = asyncio.Event()
    release = asyncio.Event()

    class BlockingReader(ScriptedEndpointReader):
        async def fetch(self, endpoint: EndpointRuntimeSnapshot) -> EndpointPollOutcome:
            first_seen.set()
            await release.wait()
            return _success(endpoint, [_alert("race-fp")])

    coordinator = PollCoordinator(f20_engine, endpoint_reader=BlockingReader({}))
    task = asyncio.create_task(
        coordinator.poll_source_id(
            "src_race", now=datetime(2026, 7, 22, 1, 30, tzinfo=timezone.utc)
        )
    )
    await asyncio.wait_for(first_seen.wait(), timeout=1)
    with Session(f20_engine) as session:
        disable_event_source(session, "src_race", expected_version=1)
        session.commit()
    release.set()

    outcome = await task

    assert outcome.committed is False
    assert outcome.error_code == "SOURCE_CONFIG_CHANGED_DURING_POLL"
    with Session(f20_engine) as session:
        assert session.exec(select(SourcePollRun)).all() == []
        assert session.exec(select(Alert)).all() == []


@pytest.mark.asyncio
async def test_coordinator_applies_source_locks_and_global_bounded_concurrency(
    f20_engine,
) -> None:
    with Session(f20_engine) as session:
        _create_source(session, "src_a", endpoint_count=2)
        _create_source(session, "src_b", endpoint_count=2)

    scripts: dict[str, list[EndpointPollOutcome]] = {}
    for source_id in ("src_a", "src_b"):
        for position in range(2):
            endpoint = _endpoint(position, source_id=source_id)
            url = f"https://{source_id}-ep-{position}.invalid"
            scripts[url] = [_success(endpoint, [_alert(f"{source_id}-{position}")])]
    reader = ScriptedEndpointReader(scripts, delay=0.02)
    coordinator = PollCoordinator(
        f20_engine,
        endpoint_reader=reader,
        max_concurrent_endpoint_polls=2,
    )

    await coordinator.poll_enabled_sources(
        now=datetime(2026, 7, 22, 1, 5, tzinfo=timezone.utc)
    )

    assert len(reader.calls) == 4
    assert reader.max_active <= 2

    source_scripts = {
        "https://src_a-ep-0.invalid": [
            _success(_endpoint(0, source_id="src_a"), [_alert("serial-a")]),
            _success(_endpoint(0, source_id="src_a"), [_alert("serial-a")]),
        ],
        "https://src_a-ep-1.invalid": [
            _success(_endpoint(1, source_id="src_a"), []),
            _success(_endpoint(1, source_id="src_a"), []),
        ],
    }
    serial_reader = ScriptedEndpointReader(source_scripts, delay=0.02)
    serial = PollCoordinator(
        f20_engine,
        endpoint_reader=serial_reader,
        max_concurrent_endpoint_polls=4,
    )
    await asyncio.gather(
        serial.poll_source_id(
            "src_a", now=datetime(2026, 7, 22, 1, 6, tzinfo=timezone.utc)
        ),
        serial.poll_source_id(
            "src_a", now=datetime(2026, 7, 22, 1, 7, tzinfo=timezone.utc)
        ),
    )
    assert serial_reader.max_active <= 2


@pytest.mark.asyncio
async def test_one_source_transaction_rollback_does_not_pollute_other_source(
    f20_engine, monkeypatch
) -> None:
    with Session(f20_engine) as session:
        _create_source(session, "src_a")
        _create_source(session, "src_b")

    scripts = {
        "https://src_a-ep-0.invalid": [
            _success(_endpoint(0, source_id="src_a"), [_alert("a-fp", cluster="a")])
        ],
        "https://src_b-ep-0.invalid": [
            _success(_endpoint(0, source_id="src_b"), [_alert("b-fp", cluster="b")])
        ],
    }
    real_ingest = source_polling.ingest_alerts

    def flaky_ingest(session: Session, raw_alerts: list[dict[str, Any]], **kwargs):
        result = real_ingest(session, raw_alerts, **kwargs)
        if kwargs["source_id"] == "src_a":
            raise RuntimeError("source a transaction failed")
        return result

    monkeypatch.setattr(source_polling, "ingest_alerts", flaky_ingest)
    coordinator = PollCoordinator(
        f20_engine,
        endpoint_reader=ScriptedEndpointReader(scripts),
    )

    outcomes = await coordinator.poll_enabled_sources(
        now=datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
    )

    assert {item.source_id: item.committed for item in outcomes} == {
        "src_a": False,
        "src_b": True,
    }
    with Session(f20_engine) as session:
        alerts = session.exec(select(Alert).order_by(Alert.source_id)).all()
        assert [(item.source_id, item.upstream_fingerprint) for item in alerts] == [
            ("src_b", "b-fp")
        ]
        assert session.exec(select(Incident).where(Incident.source_id == "src_a")).all() == []
        assert session.exec(select(SourcePollRun)).one().source_id == "src_b"


@pytest.mark.asyncio
async def test_partial_and_failed_polls_do_not_advance_resolution_or_notifications(
    f20_engine,
) -> None:
    with Session(f20_engine) as session:
        _create_source(session, "src_gate", endpoint_count=2, resolution_grace_seconds=0)
        await _configure_fake_notification_policy(session)

    now = datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc)
    scripts = {
        "https://src_gate-ep-0.invalid": [
            _success(_endpoint(0, source_id="src_gate"), [_alert("gate-fp")]),
            _failure(_endpoint(0, source_id="src_gate"), "ENDPOINT_TIMEOUT"),
            _failure(_endpoint(0, source_id="src_gate"), "ENDPOINT_NETWORK"),
            _success(_endpoint(0, source_id="src_gate"), []),
            _success(_endpoint(0, source_id="src_gate"), []),
        ],
        "https://src_gate-ep-1.invalid": [
            _success(_endpoint(1, source_id="src_gate"), [_alert("gate-fp")]),
            _success(_endpoint(1, source_id="src_gate"), []),
            _failure(_endpoint(1, source_id="src_gate"), "ENDPOINT_TIMEOUT"),
            _success(_endpoint(1, source_id="src_gate"), []),
            _success(_endpoint(1, source_id="src_gate"), []),
        ],
    }
    coordinator = PollCoordinator(
        f20_engine,
        endpoint_reader=ScriptedEndpointReader(scripts),
    )

    await coordinator.poll_source_id("src_gate", now=now)
    with Session(f20_engine) as session:
        incident = session.exec(select(Incident)).one()
        incident.handling_state = "IN_PROGRESS"
        session.add(incident)
        for target in session.exec(select(NotificationRouteTarget)).all():
            target.opened_success_at = now
            session.add(target)
        session.commit()
        occurrence_no = incident.occurrence_no
        delivery_count = len(session.exec(select(NotificationDelivery)).all())

    await coordinator.poll_source_id("src_gate", now=now + timedelta(minutes=1))
    await coordinator.poll_source_id("src_gate", now=now + timedelta(minutes=2))

    with Session(f20_engine) as session:
        alert = session.exec(select(Alert)).one()
        incident = session.exec(select(Incident)).one()
        assert alert.source_state == "firing"
        assert alert.missing_since_at is None
        assert incident.source_state == "firing"
        assert incident.occurrence_no == occurrence_no
        assert incident.handling_state == "IN_PROGRESS"
        deliveries = session.exec(select(NotificationDelivery)).all()
        assert len(deliveries) == delivery_count
        assert {item.event_type for item in deliveries} == {"FIRING_OPENED"}
        runs = session.exec(
            select(SourcePollRun).order_by(SourcePollRun.started_at)
        ).all()
        assert [item.completeness for item in runs] == [
            "COMPLETE",
            "PARTIAL",
            "FAILED",
        ]
        assert session.exec(select(AlertEndpointObservation)).all()

    await coordinator.poll_source_id("src_gate", now=now + timedelta(minutes=3))
    with Session(f20_engine) as session:
        assert session.exec(select(Alert)).one().source_state == "pending_resolution"
        assert session.exec(select(Incident)).one().source_state == "pending_resolution"

    await coordinator.poll_source_id("src_gate", now=now + timedelta(minutes=4))
    with Session(f20_engine) as session:
        assert session.exec(select(Alert)).one().source_state == "resolved"
        assert session.exec(select(Incident)).one().source_state == "recovered"
        endpoint_results = session.exec(select(EndpointPollResult)).all()
        assert {item.status for item in endpoint_results} >= {
            "SUCCESS",
            "TIMEOUT",
            "NETWORK",
        }


@pytest.mark.asyncio
async def test_partial_poll_after_reenable_does_not_clear_stale_freshness(
    f20_engine,
) -> None:
    from app.services.event_sources import disable_event_source, enable_event_source

    with Session(f20_engine) as session:
        _create_source(session, "src_stale", endpoint_count=2)

    now = datetime(2026, 7, 22, 3, 30, tzinfo=timezone.utc)
    scripts = {
        "https://src_stale-ep-0.invalid": [
            _success(_endpoint(0, source_id="src_stale"), [_alert("stale-fp")]),
            _success(_endpoint(0, source_id="src_stale"), [_alert("stale-fp")]),
        ],
        "https://src_stale-ep-1.invalid": [
            _success(_endpoint(1, source_id="src_stale"), [_alert("stale-fp")]),
            _failure(_endpoint(1, source_id="src_stale"), "ENDPOINT_TIMEOUT"),
        ],
    }
    coordinator = PollCoordinator(
        f20_engine, endpoint_reader=ScriptedEndpointReader(scripts)
    )
    await coordinator.poll_source_id("src_stale", now=now)
    with Session(f20_engine) as session:
        disable_event_source(session, "src_stale", expected_version=1)
        enable_event_source(session, "src_stale", expected_version=2)
        session.commit()
        assert session.exec(select(Incident)).one().freshness_state == "STALE"

    outcome = await coordinator.poll_source_id(
        "src_stale", now=now + timedelta(minutes=1)
    )

    assert outcome.completeness == "PARTIAL"
    with Session(f20_engine) as session:
        assert session.exec(select(Incident)).one().freshness_state == "STALE"


def test_health_reports_per_source_poll_status_and_success_times(f20_engine) -> None:
    complete_at = datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)
    partial_at = complete_at + timedelta(minutes=1)
    failed_at = complete_at + timedelta(minutes=2)
    with Session(f20_engine) as session:
        source_a = _create_source(session, "src_health_a")
        source_b = _create_source(session, "src_health_b")
        session.add(
            EventSource(
                id="src_disabled",
                name="Disabled source",
                lifecycle_state="DISABLED",
            )
        )
        session.add(
            SourcePollRun(
                source_id=source_a.id,
                source_revision_id=int(source_a.active_revision_id),
                started_at=complete_at,
                finished_at=complete_at,
                completeness="COMPLETE",
                endpoint_total=2,
                endpoint_succeeded=2,
                endpoint_failed=0,
                normalized_alert_count=2,
                safe_error_codes=[],
            )
        )
        session.add(
            SourcePollRun(
                source_id=source_a.id,
                source_revision_id=int(source_a.active_revision_id),
                started_at=partial_at,
                finished_at=partial_at,
                completeness="PARTIAL",
                endpoint_total=2,
                endpoint_succeeded=1,
                endpoint_failed=1,
                normalized_alert_count=1,
                safe_error_codes=["ENDPOINT_TIMEOUT", "PARTIAL_POLL"],
            )
        )
        session.add(
            SourcePollRun(
                source_id=source_b.id,
                source_revision_id=int(source_b.active_revision_id),
                started_at=failed_at,
                finished_at=failed_at,
                completeness="FAILED",
                endpoint_total=1,
                endpoint_succeeded=0,
                endpoint_failed=1,
                normalized_alert_count=0,
                safe_error_codes=["ALL_ENDPOINTS_FAILED", "ENDPOINT_NETWORK"],
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(health.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: Session(f20_engine)
    body = TestClient(app).get("/api/health").json()

    by_source = {item["source_id"]: item for item in body["sources"]}
    assert by_source["src_health_a"]["health"] == "DEGRADED"
    assert by_source["src_health_a"]["last_poll_at"] == "2026-07-22T04:01:00Z"
    assert by_source["src_health_a"]["last_complete_success_at"] == (
        "2026-07-22T04:00:00Z"
    )
    assert by_source["src_health_a"]["last_any_success_at"] == (
        "2026-07-22T04:01:00Z"
    )
    assert by_source["src_health_b"]["health"] == "UNAVAILABLE"
    assert by_source["src_health_b"]["last_any_success_at"] is None
    assert by_source["src_disabled"]["health"] == "DISABLED"
