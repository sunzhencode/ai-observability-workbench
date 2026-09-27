"""F20 source-level Watchdog inventory and health derivation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.registry_models import (
    EventSource,
    EventSourceRevision,
    MonitoredCluster,
    SourcePollRun,
)

WATCHDOG_HEALTH_ORDER = {"MISSING": 0, "UNKNOWN": 1, "HEALTHY": 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _f20_tables_available(session: Session) -> bool:
    # `inspect(session.connection())`, not `inspect(engine)`: the engine form
    # borrows a pooled connection, which under StaticPool is this session's own,
    # and handing it back discards anything the caller flushed. This module is
    # reached from inside the poll transaction, right after a flush.
    return bool(inspect(session.connection()).has_table("monitoredcluster"))


def missing_identity_value(identity_label: str) -> str:
    return f"<no-{identity_label}>"


def is_watchdog_alert(raw_alert: dict[str, Any], alertname: str) -> bool:
    labels = raw_alert.get("labels") if isinstance(raw_alert.get("labels"), dict) else {}
    return str(labels.get("alertname") or "") == alertname


def watchdog_identity(raw_alert: dict[str, Any], identity_label: str) -> str:
    labels = raw_alert.get("labels") if isinstance(raw_alert.get("labels"), dict) else {}
    value = str(labels.get(identity_label) or "").strip()
    return value if value else missing_identity_value(identity_label)


def record_watchdog_observations(
    session: Session,
    *,
    source_id: str,
    watchdog_enabled: bool,
    watchdog_alertname: str,
    watchdog_identity_label: str,
    raw_alerts: list[dict[str, Any]],
    observed_at: datetime,
) -> set[str]:
    """Persist discovered Watchdog inventory without changing ignored/expected state."""

    if not watchdog_enabled:
        return set()
    identities = {
        watchdog_identity(raw, watchdog_identity_label)
        for raw in raw_alerts
        if is_watchdog_alert(raw, watchdog_alertname)
    }
    for identity in sorted(identities):
        row = session.exec(
            select(MonitoredCluster).where(
                MonitoredCluster.source_id == source_id,
                MonitoredCluster.identity_value == identity,
            )
        ).first()
        if row is None:
            row = MonitoredCluster(
                source_id=source_id,
                identity_value=identity,
                inventory_state="DISCOVERED",
                first_discovered_at=observed_at,
                monitoring_started_at=observed_at,
                last_seen_at=observed_at,
            )
        else:
            row.last_seen_at = observed_at
        session.add(row)
    session.flush()
    return identities


def _source(session: Session, source_id: str) -> EventSource:
    source = session.get(EventSource, source_id)
    if source is None:
        raise LookupError("event source not found")
    return source


def _active_revision(
    session: Session, source: EventSource
) -> EventSourceRevision | None:
    if source.active_revision_id is None:
        return None
    revision = session.get(EventSourceRevision, source.active_revision_id)
    if revision is None or revision.internal_state != "ACTIVE":
        return None
    return revision


def add_expected_cluster(
    session: Session, source_id: str, identity_value: str, *, now: datetime | None = None
) -> MonitoredCluster:
    source = _source(session, source_id)
    if source.lifecycle_state == "ARCHIVED":
        raise ValueError("archived source inventory is read-only")
    value = str(identity_value or "").strip()
    if not value or len(value) > 256:
        raise ValueError("identity_value must contain 1-256 characters")
    observed_at = now or _now()
    row = session.exec(
        select(MonitoredCluster).where(
            MonitoredCluster.source_id == source_id,
            MonitoredCluster.identity_value == value,
        )
    ).first()
    if row is None:
        row = MonitoredCluster(
            source_id=source_id,
            identity_value=value,
            inventory_state="EXPECTED",
            first_discovered_at=observed_at,
            monitoring_started_at=observed_at,
        )
    elif row.inventory_state != "EXPECTED":
        row.inventory_state = "EXPECTED"
        row.monitoring_started_at = observed_at
        row.ignored_at = None
        row.ignored_reason = None
        row.version += 1
    session.add(row)
    session.flush()
    return row


def update_cluster_inventory_state(
    session: Session,
    source_id: str,
    cluster_id: int,
    *,
    action: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> MonitoredCluster:
    source = _source(session, source_id)
    if source.lifecycle_state == "ARCHIVED":
        raise ValueError("archived source inventory is read-only")
    cluster = session.get(MonitoredCluster, cluster_id)
    if cluster is None or cluster.source_id != source_id:
        raise LookupError("monitored cluster not found")
    observed_at = now or _now()
    normalized = action.upper()
    if normalized in {"EXPECT", "RESTORE"}:
        cluster.inventory_state = "EXPECTED"
        cluster.monitoring_started_at = observed_at
        cluster.ignored_at = None
        cluster.ignored_reason = None
    elif normalized == "IGNORE":
        cluster.inventory_state = "IGNORED"
        cluster.ignored_at = observed_at
        cluster.ignored_reason = str(reason or "").strip() or None
    else:
        raise ValueError("unsupported watchdog cluster action")
    cluster.version += 1
    session.add(cluster)
    session.flush()
    return cluster


def _poll_runs(session: Session, source_id: str) -> list[SourcePollRun]:
    return list(
        session.exec(
            select(SourcePollRun)
            .where(SourcePollRun.source_id == source_id)
            .order_by(SourcePollRun.started_at.desc(), SourcePollRun.id.desc())
        ).all()
    )


def _latest_observed_identities(
    clusters: list[MonitoredCluster], latest_run: SourcePollRun | None
) -> set[str]:
    """Which clusters this run actually saw.

    `record_watchdog_observations` stamps `last_seen_at` with the run's own
    `poll_time`, which is the same value as `SourcePollRun.started_at`, so
    "seen at or after this run began" is exact. It used to be a one-second
    tolerance around started_at/finished_at, which quietly turned into an
    approximate equality test on timestamps -- the pattern this codebase bans
    for round membership, and one that reports a stale cluster as healthy
    whenever two runs land inside the same second.
    """
    if latest_run is None:
        return set()
    started_at = _aware(latest_run.started_at)
    return {
        cluster.identity_value
        for cluster in clusters
        if cluster.last_seen_at is not None
        and _aware(cluster.last_seen_at) >= started_at
    }


def _complete_streak_start(runs: list[SourcePollRun]) -> datetime | None:
    if not runs or runs[0].completeness != "COMPLETE":
        return None
    complete_runs: list[SourcePollRun] = []
    for run in runs:
        if run.completeness != "COMPLETE":
            break
        complete_runs.append(run)
    return min(item.started_at for item in complete_runs)


def _cluster_health(
    cluster: MonitoredCluster,
    *,
    source: EventSource,
    revision: EventSourceRevision | None,
    runs: list[SourcePollRun],
    observed_latest: set[str],
    now: datetime,
) -> str | None:
    if cluster.inventory_state == "IGNORED":
        return None
    if source.lifecycle_state != "ENABLED" or revision is None:
        return None
    if not revision.watchdog_enabled:
        return None
    latest = runs[0] if runs else None
    if latest is None or latest.completeness == "FAILED":
        return "UNKNOWN"
    if latest.completeness == "PARTIAL":
        return "HEALTHY" if cluster.identity_value in observed_latest else "UNKNOWN"
    if cluster.identity_value in observed_latest:
        return "HEALTHY"
    streak_start = _complete_streak_start(runs) or latest.started_at
    last_seen = cluster.last_seen_at
    basis = max(_aware(streak_start), _aware(cluster.monitoring_started_at))
    if last_seen is not None and _aware(last_seen) >= basis:
        basis = _aware(last_seen)
    missing_after = max(
        revision.poll_interval_seconds,
        revision.watchdog_missing_after_seconds,
    )
    if _aware(now) >= basis + timedelta(seconds=missing_after):
        return "MISSING"
    return "HEALTHY"


def _source_health_from_latest(
    source: EventSource, latest: SourcePollRun | None
) -> str:
    if source.lifecycle_state == "DISABLED":
        return "DISABLED"
    if source.lifecycle_state == "ARCHIVED":
        return "ARCHIVED"
    if latest is None or latest.completeness == "FAILED":
        return "UNAVAILABLE"
    if latest.completeness == "PARTIAL":
        return "DEGRADED"
    return "HEALTHY"


def _cluster_public(
    cluster: MonitoredCluster,
    *,
    health_state: str | None,
    still_emitting: bool,
    now: datetime,
) -> dict[str, Any]:
    status = health_state.lower() if health_state is not None else "unknown"
    freshness = (
        max(0, int((_aware(now) - _aware(cluster.last_seen_at)).total_seconds()))
        if cluster.last_seen_at is not None
        else None
    )
    return {
        "id": cluster.id,
        "source_id": cluster.source_id,
        "identity_value": cluster.identity_value,
        "cluster": cluster.identity_value,
        "inventory_state": cluster.inventory_state,
        "health_state": health_state,
        "status": status,
        "first_discovered_at": cluster.first_discovered_at,
        "monitoring_started_at": cluster.monitoring_started_at,
        "last_seen_at": cluster.last_seen_at,
        "ignored_at": cluster.ignored_at,
        "ignored_reason": cluster.ignored_reason,
        "still_emitting": still_emitting,
        "freshness_seconds": freshness,
        "version": cluster.version,
    }


def derive_watchdog_health(
    session: Session, *, now: datetime | None = None
) -> dict[str, Any]:
    if not _f20_tables_available(session):
        return {
            "monitored_source_count": 0,
            "unmonitored_source_count": 0,
            "summary": {"total": 0, "healthy": 0, "missing": 0, "unknown": 0},
            "sources": [],
            "clusters": [],
            "overall_status": "no_data",
        }
    observed_at = now or _now()
    sources = list(
        session.exec(select(EventSource).order_by(EventSource.name, EventSource.id)).all()
    )
    monitored_source_count = 0
    unmonitored_source_count = 0
    global_counts = {"HEALTHY": 0, "MISSING": 0, "UNKNOWN": 0}
    source_results: list[dict[str, Any]] = []
    flattened: list[dict[str, Any]] = []

    for source in sources:
        revision = _active_revision(session, source)
        runs = _poll_runs(session, source.id)
        latest = runs[0] if runs else None
        clusters = list(
            session.exec(
                select(MonitoredCluster)
                .where(MonitoredCluster.source_id == source.id)
                .order_by(MonitoredCluster.identity_value)
            ).all()
        )
        monitor_enabled = (
            source.lifecycle_state == "ENABLED"
            and revision is not None
            and revision.watchdog_enabled
        )
        if monitor_enabled:
            monitored_source_count += 1
            monitor_state = "ENABLED"
        else:
            unmonitored_source_count += 1
            monitor_state = (
                "SOURCE_DISABLED"
                if source.lifecycle_state == "DISABLED"
                else "SOURCE_ARCHIVED"
                if source.lifecycle_state == "ARCHIVED"
                else "DISABLED"
            )
        observed_latest = _latest_observed_identities(clusters, latest)
        counts = {"HEALTHY": 0, "MISSING": 0, "UNKNOWN": 0}
        cluster_items: list[dict[str, Any]] = []
        for cluster in clusters:
            health_state = _cluster_health(
                cluster,
                source=source,
                revision=revision,
                runs=runs,
                observed_latest=observed_latest,
                now=observed_at,
            )
            still_emitting = (
                cluster.inventory_state == "IGNORED"
                and cluster.identity_value in observed_latest
            )
            item = _cluster_public(
                cluster,
                health_state=health_state,
                still_emitting=still_emitting,
                now=observed_at,
            )
            cluster_items.append(item)
            if health_state is not None:
                counts[health_state] += 1
                global_counts[health_state] += 1
                flattened.append(item)
        cluster_items.sort(
            key=lambda item: (
                WATCHDOG_HEALTH_ORDER.get(item["health_state"] or "UNKNOWN", 9),
                item["identity_value"],
            )
        )
        source_results.append(
            {
                "source_id": source.id,
                "source_name": source.name,
                "source_health": _source_health_from_latest(source, latest),
                "monitor_state": monitor_state,
                "summary": {
                    "total": sum(counts.values()),
                    "healthy": counts["HEALTHY"],
                    "missing": counts["MISSING"],
                    "unknown": counts["UNKNOWN"],
                },
                "clusters": cluster_items,
            }
        )
    flattened.sort(
        key=lambda item: (
            WATCHDOG_HEALTH_ORDER.get(item["health_state"] or "UNKNOWN", 9),
            item["source_id"],
            item["identity_value"],
        )
    )
    summary = {
        "total": sum(global_counts.values()),
        "healthy": global_counts["HEALTHY"],
        "missing": global_counts["MISSING"],
        "unknown": global_counts["UNKNOWN"],
    }
    overall = (
        "no_data"
        if summary["total"] == 0
        else "missing"
        if summary["missing"]
        else "unknown"
        if summary["unknown"]
        else "healthy"
    )
    return {
        "monitored_source_count": monitored_source_count,
        "unmonitored_source_count": unmonitored_source_count,
        "summary": summary,
        "sources": source_results,
        "clusters": flattened,
        "overall_status": overall,
    }


def list_watchdog_clusters(
    session: Session, source_id: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    _source(session, source_id)
    derived = derive_watchdog_health(session, now=now)
    for source in derived["sources"]:
        if source["source_id"] == source_id:
            return list(source["clusters"])
    return []
