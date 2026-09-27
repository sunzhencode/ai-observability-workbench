"""Source and alerting vertical-slice contracts on the isolated platform store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import inspect, select

from app.adapters.persistence.sources import (
    AlertRecord,
    IncidentRecord,
    PollRunRecord,
    SqlAlchemySourceStore,
    WatchdogClusterRecord,
)
from app.adapters.persistence.jobs import EventRecord
from app.application.sources import (
    ApplyCollection,
    CollectSource,
    EndpointDraft,
    SourceJobPlanner,
    SourceDraft,
)
from app.domains.sources.models import EndpointObservation, PollCompleteness
from app.domains.sources.models import SourceState
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database

UTC = timezone.utc


class ScriptedReader:
    def __init__(self, scripts: dict[int, EndpointObservation]) -> None:
        self.scripts = scripts
        self.calls: list[int] = []

    async def fetch(self, endpoint):
        self.calls.append(endpoint.position)
        return self.scripts[endpoint.position]


def _raw(fingerprint: str, *, alertname: str = "TargetDown") -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": alertname,
            "severity": "warning",
            "cluster": "cluster-a",
        },
        "annotations": {"summary": fingerprint},
        "startsAt": "2026-08-11T00:00:00Z",
        "updatedAt": "2026-08-11T00:01:00Z",
    }


def _store(tmp_path: Path) -> tuple[SqlAlchemySourceStore, object]:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "incident-operations.db"))
    upgrade_database(engine)
    return SqlAlchemySourceStore(create_session_factory(engine)), engine


def _source(store: SqlAlchemySourceStore) -> None:
    store.create_source(
        SourceDraft(
            id="src-a",
            name="Primary AM",
            endpoints=(
                EndpointDraft(0, "https://am-a.invalid"),
                EndpointDraft(1, "https://am-b.invalid"),
            ),
            poll_interval_seconds=30,
            resolution_grace_seconds=0,
            max_parallel_endpoints=2,
            watchdog_enabled=True,
            watchdog_alertname="Watchdog",
            watchdog_identity_label="cluster",
            watchdog_missing_after_seconds=90,
        ),
        now=datetime(2026, 8, 11, tzinfo=UTC),
    )


async def _collect_apply(
    store: SqlAlchemySourceStore,
    scripts: dict[int, EndpointObservation],
    *,
    now: datetime,
) -> PollCompleteness:
    snapshot = store.load_snapshot("src-a", expected_version=1)
    outcome = await CollectSource(store, ScriptedReader(scripts)).execute(
        "src-a", expected_version=1
    )
    result = ApplyCollection(store).execute(snapshot, outcome, observed_at=now)
    assert result.committed is True
    return result.completeness


async def test_complete_partial_failed_and_watchdog_inventory(tmp_path: Path) -> None:
    store, engine = _store(tmp_path)
    _source(store)
    snapshot = store.load_snapshot("src-a", expected_version=1)
    t0 = datetime(2026, 8, 11, 1, tzinfo=UTC)

    complete = {
        0: EndpointObservation(
            snapshot.endpoints[0],
            "SUCCESS",
            (_raw("target"), _raw("wd", alertname="Watchdog")),
            4,
        ),
        1: EndpointObservation(snapshot.endpoints[1], "SUCCESS", (), 5),
    }
    assert await _collect_apply(store, complete, now=t0) is PollCompleteness.COMPLETE

    partial = {
        0: EndpointObservation(snapshot.endpoints[0], "SUCCESS", (), 4),
        1: EndpointObservation(
            snapshot.endpoints[1],
            "TIMEOUT",
            (),
            10,
            safe_error_code="ENDPOINT_TIMEOUT",
        ),
    }
    assert await _collect_apply(store, partial, now=t0 + timedelta(minutes=1)) is (
        PollCompleteness.PARTIAL
    )

    failed = {
        0: EndpointObservation(
            snapshot.endpoints[0],
            "NETWORK",
            (),
            10,
            safe_error_code="ENDPOINT_NETWORK",
        ),
        1: EndpointObservation(
            snapshot.endpoints[1],
            "TIMEOUT",
            (),
            10,
            safe_error_code="ENDPOINT_TIMEOUT",
        ),
    }
    assert await _collect_apply(store, failed, now=t0 + timedelta(minutes=2)) is (
        PollCompleteness.FAILED
    )

    sessions = create_session_factory(engine)
    with sessions() as session:
        alert = session.scalar(select(AlertRecord))
        incident = session.scalar(select(IncidentRecord))
        watchdog = session.scalar(select(WatchdogClusterRecord))
        runs = list(session.scalars(select(PollRunRecord).order_by(PollRunRecord.id)))
        assert alert is not None and alert.source_state == "FIRING"
        assert incident is not None and incident.source_state == "FIRING"
        assert watchdog is not None and watchdog.identity_value == "cluster-a"
        assert [item.completeness for item in runs] == ["COMPLETE", "PARTIAL", "FAILED"]


async def test_version_fence_and_disable_stale_do_not_fabricate_recovery(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path)
    _source(store)
    snapshot = store.load_snapshot("src-a", expected_version=1)
    initial = await CollectSource(
        store,
        ScriptedReader(
            {
                item.position: EndpointObservation(
                    item,
                    "SUCCESS",
                    (_raw("target"),) if item.position == 0 else (),
                    4,
                )
                for item in snapshot.endpoints
            }
        ),
    ).execute("src-a", expected_version=1)
    assert ApplyCollection(store).execute(
        snapshot,
        initial,
        observed_at=datetime(2026, 8, 11, 1, tzinfo=UTC),
    ).committed is True

    in_flight_snapshot = store.load_snapshot("src-a", expected_version=1)
    in_flight = await CollectSource(
        store,
        ScriptedReader(
            {
                item.position: EndpointObservation(item, "SUCCESS", (), 4)
                for item in in_flight_snapshot.endpoints
            }
        ),
    ).execute("src-a", expected_version=1)

    store.disable_source("src-a", expected_version=1, now=datetime.now(UTC))
    rejected = ApplyCollection(store).execute(
        in_flight_snapshot,
        in_flight,
        observed_at=datetime.now(UTC),
    )
    assert rejected.committed is False
    assert rejected.safe_error_code == "SOURCE_CONFIG_CHANGED_DURING_POLL"

    sessions = create_session_factory(engine)
    with sessions() as session:
        alert = session.scalar(select(AlertRecord))
        incident = session.scalar(select(IncidentRecord))
        assert alert is not None and alert.source_state == "FIRING"
        assert incident is not None and incident.source_state == "FIRING"
        assert incident.freshness_state == "STALE"
        assert len(list(session.scalars(select(PollRunRecord)))) == 1

    assert [item.action for item in store.list_source_audit("src-a")] == [
        "CREATED",
        "DISABLED",
    ]


async def test_recovered_incident_reopens_only_on_positive_live_observation(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path)
    _source(store)
    t0 = datetime(2026, 8, 11, 1, tzinfo=UTC)
    snapshot = store.load_snapshot("src-a", expected_version=1)

    async def apply(alerts: tuple[dict[str, object], ...], at: datetime, *, partial=False):
        scripts = {
            0: EndpointObservation(snapshot.endpoints[0], "SUCCESS", alerts, 2),
            1: (
                EndpointObservation(
                    snapshot.endpoints[1],
                    "TIMEOUT",
                    (),
                    2,
                    safe_error_code="ENDPOINT_TIMEOUT",
                )
                if partial
                else EndpointObservation(snapshot.endpoints[1], "SUCCESS", (), 2)
            ),
        }
        outcome = await CollectSource(store, ScriptedReader(scripts)).execute(
            "src-a", expected_version=1
        )
        return ApplyCollection(store).execute(snapshot, outcome, observed_at=at)

    await apply((_raw("target"),), t0)
    await apply((), t0 + timedelta(minutes=1))
    await apply((), t0 + timedelta(minutes=2))

    sessions = create_session_factory(engine)
    with sessions() as session:
        incident = session.scalar(select(IncidentRecord))
        assert incident is not None and incident.source_state == "RECOVERED"
        assert incident.occurrence_no == 1

    await apply((_raw("target"),), t0 + timedelta(minutes=3), partial=True)
    with sessions() as session:
        incident = session.scalar(select(IncidentRecord))
        assert incident is not None and incident.source_state == "FIRING"
        assert incident.occurrence_no == 2


async def test_watchdog_partial_observed_is_healthy_unobserved_is_unknown(
    tmp_path: Path,
) -> None:
    store, _engine = _store(tmp_path)
    _source(store)
    t0 = datetime(2026, 8, 11, 1, tzinfo=UTC)
    store.add_expected_watchdog_cluster("src-a", "cluster-b", now=t0)
    snapshot = store.load_snapshot("src-a", expected_version=1)
    outcome = await CollectSource(
        store,
        ScriptedReader(
            {
                0: EndpointObservation(
                    snapshot.endpoints[0],
                    "SUCCESS",
                    (_raw("wd", alertname="Watchdog"),),
                    2,
                ),
                1: EndpointObservation(
                    snapshot.endpoints[1],
                    "TIMEOUT",
                    (),
                    2,
                    safe_error_code="ENDPOINT_TIMEOUT",
                ),
            }
        ),
    ).execute("src-a", expected_version=1)
    ApplyCollection(store).execute(snapshot, outcome, observed_at=t0)

    health = {
        item.identity_value: item.health_state
        for item in store.list_watchdog_clusters("src-a", now=t0)
    }
    assert health == {"cluster-a": "HEALTHY", "cluster-b": "UNKNOWN"}


def test_rule_publish_regroups_without_notification_event(tmp_path: Path) -> None:
    store, engine = _store(tmp_path)
    _source(store)
    store.seed_alert_for_characterization(
        source_id="src-a",
        raw=_raw("one"),
        observed_at=datetime(2026, 8, 11, 1, tzinfo=UTC),
    )
    preview = store.preview_rule(
        name="target by cluster",
        priority=10,
        enabled=True,
        matchers=(("alertname", "=", "TargetDown"),),
        group_by_labels=("cluster",),
        source_ids=("src-a",),
    )
    assert preview.matcher_alert_count == 1
    assert preview.selected_alert_count == 1
    assert preview.proposed_group_count == 1
    assert preview.groups[0].fingerprints == ("one",)
    assert preview.by_source[0].source_id == "src-a"
    assert preview.by_source[0].selected_alert_count == 1

    rule = store.publish_rule(
        name="target by cluster",
        priority=10,
        enabled=True,
        matchers=(("alertname", "=", "TargetDown"),),
        group_by_labels=("cluster",),
        source_ids=("src-a",),
        now=datetime(2026, 8, 11, 2, tzinfo=UTC),
    )
    assert rule.version == 1

    sessions = create_session_factory(engine)
    with sessions() as session:
        incident = session.scalar(
            select(IncidentRecord).where(IncidentRecord.aggregation_rule_id == rule.id)
        )
        assert incident is not None
        event_types = list(session.scalars(select(EventRecord.event_type)))
        assert not any("notification" in event_type for event_type in event_types)


def test_current_source_config_updates_in_place_and_lifecycle_is_audited(
    tmp_path: Path,
) -> None:
    store, _engine = _store(tmp_path)
    _source(store)
    updated = store.update_source(
        "src-a",
        SourceDraft(
            id="src-a",
            name="Renamed AM",
            endpoints=(EndpointDraft(3, "https://replacement.invalid"),),
            poll_interval_seconds=60,
            resolution_grace_seconds=120,
            max_parallel_endpoints=1,
        ),
        expected_version=1,
        now=datetime(2026, 8, 11, 1, tzinfo=UTC),
    )
    assert updated.id == "src-a"
    assert updated.version == 2
    assert updated.name == "Renamed AM"
    assert [item.position for item in updated.endpoints] == [3]

    disabled = store.set_source_state(
        "src-a",
        target=SourceState.DISABLED,
        expected_version=2,
        now=datetime(2026, 8, 11, 2, tzinfo=UTC),
    )
    enabled = store.set_source_state(
        "src-a",
        target=SourceState.ENABLED,
        expected_version=3,
        now=datetime(2026, 8, 11, 3, tzinfo=UTC),
    )
    archived = store.set_source_state(
        "src-a",
        target=SourceState.ARCHIVED,
        expected_version=4,
        now=datetime(2026, 8, 11, 4, tzinfo=UTC),
    )
    assert (disabled.state, enabled.state, archived.state) == (
        "DISABLED",
        "ENABLED",
        "ARCHIVED",
    )
    assert [item.action for item in store.list_source_audit("src-a")] == [
        "CREATED",
        "UPDATED",
        "DISABLED",
        "ENABLED",
        "ARCHIVED",
    ]


async def test_due_source_planner_only_returns_versioned_enqueue_specs(
    tmp_path: Path,
) -> None:
    store, _engine = _store(tmp_path)
    _source(store)
    planner = SourceJobPlanner(store)
    t0 = datetime(2026, 8, 11, 1, tzinfo=UTC)
    first = planner.plan(now=t0)
    assert len(first) == 1
    assert first[0].kind == "source.collect"
    assert first[0].payload == {"expected_version": 1}

    snapshot = store.load_snapshot("src-a", expected_version=1)
    outcome = await CollectSource(
        store,
        ScriptedReader(
            {
                item.position: EndpointObservation(item, "SUCCESS", (), 1)
                for item in snapshot.endpoints
            }
        ),
    ).execute("src-a", expected_version=1)
    ApplyCollection(store).execute(snapshot, outcome, observed_at=t0)

    assert planner.plan(now=t0 + timedelta(seconds=29)) == ()
    assert len(planner.plan(now=t0 + timedelta(seconds=30))) == 1


def test_source_alerting_migration_contains_only_candidate_business_tables(tmp_path: Path) -> None:
    _store_value, engine = _store(tmp_path)
    tables = set(inspect(engine).get_table_names())
    assert {
        "event_source",
        "source_endpoint",
        "source_audit",
        "source_poll_run",
        "endpoint_poll_result",
        "aggregation_rule",
        "alert",
        "incident",
        "watchdog_cluster",
    } <= tables
    assert "eventsource" not in tables
    assert "groupingpolicy" not in tables
