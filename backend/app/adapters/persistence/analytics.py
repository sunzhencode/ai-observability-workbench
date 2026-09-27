"""Rebuildable SQLAlchemy Analytics projection and bounded overview reader."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from typing import Any, cast

from sqlalchemy import DateTime, Index, Integer, String, Text, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.adapters.persistence.incidents import (
    IncidentTaskRecord,
    OperationalOccurrenceRecord,
)
from app.adapters.persistence.noise import NoiseLifecycleFactRecord
from app.adapters.persistence.notifications import NotificationDeliveryRecord
from app.adapters.persistence.sources import SourceRecord
from app.adapters.persistence.unified_investigations import (
    EvidenceSnapshotV2Record,
    InvestigationActivityV2Record,
    InvestigationFeedbackV2Record,
    InvestigationReportV2Record,
    InvestigationRunV2Record,
)
from app.platform.persistence.codecs import aware_utc as _aware, stored_utc as _stored
from app.application.analytics import (
    AiAnalyticsView,
    AnalyticsOverview,
    AnalyticsRefreshResult,
    DurationMetricView,
    NotificationAnalyticsView,
    ResponseAnalyticsView,
    SignalAnalyticsView,
)
from app.domains.analytics import (
    analytics_window,
    duration_summary,
    rate_summary,
    ratio_summary,
)
from app.platform.persistence.database import SessionFactory


UTC = timezone.utc
_UNMAPPED = "__UNMAPPED__"
_EXECUTION_MODES = ("FAKE", "EXTERNAL", "UNKNOWN_LEGACY")


class Base(DeclarativeBase):
    pass


class AnalyticsDailyBucketRecord(Base):
    __tablename__ = "analytics_daily_bucket"
    __table_args__ = (
        Index(
            "ix_analytics_bucket_dimensions",
            "day_utc",
            "source_id",
            "service_key",
            "signal_severity",
        ),
    )

    day_utc: Mapped[str] = mapped_column(String(10), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    service_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    signal_severity: Mapped[str] = mapped_column(String(16), primary_key=True)
    hours_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AnalyticsDurationSampleRecord(Base):
    __tablename__ = "analytics_duration_sample"
    __table_args__ = (
        Index(
            "ix_analytics_duration_filter",
            "occurred_at",
            "source_id",
            "service_key",
            "signal_severity",
            "metric",
        ),
    )

    fact_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    day_utc: Mapped[str] = mapped_column(String(10), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    service_key: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_severity: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    value_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AnalyticsRollupStateRecord(Base):
    __tablename__ = "analytics_rollup_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_complete_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _milliseconds(start: datetime, end: datetime) -> int:
    return max(0, int((_aware(end) - _aware(start)).total_seconds() * 1000))


def _counter() -> dict[str, Any]:
    return {
        "new_occurrences": 0,
        "new_alert_instances": 0,
        "creation_known": 0,
        "creation_unmapped": 0,
        "creation_unknown": 0,
        "current_unmapped": 0,
        "ack_sla_eligible": 0,
        "ack_sla_breached": 0,
        "flapping_activations": 0,
        "storm_activations": 0,
        "tasks": {},
        "resolution_codes": {},
        "notifications": {},
        "ai": {},
    }


def _increment_nested(counter: dict[str, Any], key: str, value: str) -> None:
    nested = cast(dict[str, int], counter[key])
    nested[value] = nested.get(value, 0) + 1


def _mode_counter(counter: dict[str, Any], section: str, mode: str) -> dict[str, int]:
    normalized = mode if mode in _EXECUTION_MODES else "UNKNOWN_LEGACY"
    groups = cast(dict[str, dict[str, int]], counter[section])
    return groups.setdefault(normalized, {})


def _increment_mode(
    counter: dict[str, Any], section: str, mode: str, key: str, amount: int = 1
) -> None:
    group = _mode_counter(counter, section, mode)
    group[key] = group.get(key, 0) + amount


class SqlAlchemyAnalyticsStore:
    """Refresh from local facts; overview reads only the persisted projection."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def refresh(self, *, now: datetime, rebuild_days: int = 31) -> AnalyticsRefreshResult:
        window = analytics_window("30d", now=now)
        end = window.end
        start = end - timedelta(days=rebuild_days)
        stored_start = _stored(start)
        stored_end = _stored(end)
        buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        samples: list[AnalyticsDurationSampleRecord] = []

        with self._sessions.begin() as session:
            watchdog_by_source = {
                row.id: row.watchdog_alertname
                for row in session.scalars(select(SourceRecord))
            }
            occurrences = tuple(
                session.scalars(
                    select(OperationalOccurrenceRecord).where(
                        OperationalOccurrenceRecord.detected_at.is_not(None),
                        OperationalOccurrenceRecord.detected_at >= stored_start,
                        OperationalOccurrenceRecord.detected_at < stored_end,
                    )
                )
            )
            tasks_by_occurrence: dict[int, list[IncidentTaskRecord]] = defaultdict(list)
            for task in session.scalars(
                select(IncidentTaskRecord)
                .join(
                    OperationalOccurrenceRecord,
                    OperationalOccurrenceRecord.id == IncidentTaskRecord.occurrence_id,
                )
                .where(
                    OperationalOccurrenceRecord.detected_at >= stored_start,
                    OperationalOccurrenceRecord.detected_at < stored_end,
                )
            ):
                tasks_by_occurrence[task.occurrence_id].append(task)

            for item in occurrences:
                assert item.detected_at is not None
                detected = _aware(item.detected_at)
                hour = detected.replace(minute=0, second=0, microsecond=0)
                service_key = _UNMAPPED if item.service_id is None else str(item.service_id)
                dimension = (
                    hour.date().isoformat(),
                    item.source_id,
                    service_key,
                    item.signal_severity,
                )
                hours = buckets.setdefault(dimension, {})
                hour_key = hour.isoformat().replace("+00:00", "Z")
                counter = cast(dict[str, Any], hours.setdefault(hour_key, _counter()))
                counter["new_occurrences"] += 1
                if item.primary_alertname != watchdog_by_source.get(item.source_id):
                    counter["new_alert_instances"] += max(0, item.member_count)
                if item.creation_unmapped is None:
                    counter["creation_unknown"] += 1
                else:
                    counter["creation_known"] += 1
                    if item.creation_unmapped:
                        counter["creation_unmapped"] += 1
                if item.service_id is None and item.response_state != "RESOLVED":
                    counter["current_unmapped"] += 1
                if item.ack_sla_due_at is not None:
                    due = _aware(item.ack_sla_due_at)
                    acknowledged = (
                        None if item.acknowledged_at is None else _aware(item.acknowledged_at)
                    )
                    if acknowledged is not None or due <= end:
                        counter["ack_sla_eligible"] += 1
                        if acknowledged is None or acknowledged > due:
                            counter["ack_sla_breached"] += 1
                for task in tasks_by_occurrence.get(item.id, ()):
                    _increment_nested(counter, "tasks", task.status)
                if item.resolution_code:
                    _increment_nested(counter, "resolution_codes", item.resolution_code)

                metric_values = (
                    (
                        "mtta_completed",
                        item.first_investigating_at,
                        item.first_investigating_at,
                    )
                    if item.first_investigating_at is not None
                    else ("mtta_unfinished", end, end)
                )
                samples.append(
                    AnalyticsDurationSampleRecord(
                        fact_key=f"mtta:{item.id}",
                        day_utc=hour.date().isoformat(),
                        source_id=item.source_id,
                        service_key=service_key,
                        signal_severity=item.signal_severity,
                        metric=str(metric_values[0]),
                        value_ms=_milliseconds(detected, metric_values[1]),
                        occurred_at=_stored(detected),
                    )
                )
                resolution_metric = (
                    "resolution_completed"
                    if item.resolved_at is not None
                    else "resolution_unfinished"
                )
                resolution_end = (
                    end if item.resolved_at is None else _aware(item.resolved_at)
                )
                samples.append(
                    AnalyticsDurationSampleRecord(
                        fact_key=f"resolution:{item.id}",
                        day_utc=hour.date().isoformat(),
                        source_id=item.source_id,
                        service_key=service_key,
                        signal_severity=item.signal_severity,
                        metric=resolution_metric,
                        value_ms=_milliseconds(detected, resolution_end),
                        occurred_at=_stored(detected),
                    )
                )

            delivery_rows = session.execute(
                select(
                    NotificationDeliveryRecord,
                    OperationalOccurrenceRecord.source_id,
                    OperationalOccurrenceRecord.service_id,
                    OperationalOccurrenceRecord.signal_severity,
                )
                .join(
                    OperationalOccurrenceRecord,
                    (OperationalOccurrenceRecord.incident_id == NotificationDeliveryRecord.incident_id)
                    & (OperationalOccurrenceRecord.occurrence_no == NotificationDeliveryRecord.occurrence_no),
                )
                .where(
                    NotificationDeliveryRecord.created_at >= stored_start,
                    NotificationDeliveryRecord.created_at < stored_end,
                )
            )
            for delivery, source_id, service_id, severity in delivery_rows:
                dimension_values = (
                    str(source_id),
                    _UNMAPPED if service_id is None else str(service_id),
                    str(severity),
                )
                created = _aware(delivery.created_at)
                hour = created.replace(minute=0, second=0, microsecond=0)
                dimension = (hour.date().isoformat(), *dimension_values)
                hours = buckets.setdefault(dimension, {})
                hour_key = hour.isoformat().replace("+00:00", "Z")
                counter = cast(dict[str, Any], hours.setdefault(hour_key, _counter()))
                mode = delivery.execution_mode
                if delivery.state == "SUCCEEDED":
                    _increment_mode(counter, "notifications", mode, "succeeded")
                elif delivery.state == "PERMANENTLY_FAILED":
                    _increment_mode(
                        counter, "notifications", mode, "permanently_failed"
                    )
                elif delivery.state in {"PENDING", "IN_FLIGHT", "RETRY_WAIT"}:
                    _increment_mode(counter, "notifications", mode, "pending_backlog")
                elif delivery.state in {"SUPPRESSED", "CANCELED"}:
                    _increment_mode(counter, "notifications", mode, "noise")
                if delivery.succeeded_at is not None:
                    samples.append(
                        AnalyticsDurationSampleRecord(
                            fact_key=f"delivery:{delivery.id}",
                            day_utc=hour.date().isoformat(),
                            source_id=dimension_values[0],
                            service_key=dimension_values[1],
                            signal_severity=dimension_values[2],
                            metric=f"delivery_latency:{mode}",
                            value_ms=_milliseconds(
                                created, _aware(delivery.succeeded_at)
                            ),
                            occurred_at=_stored(created),
                        )
                    )

            first_tool_at = {
                str(investigation_id): created_at
                for investigation_id, created_at in session.execute(
                    select(
                        InvestigationActivityV2Record.investigation_id,
                        func.min(InvestigationActivityV2Record.created_at),
                    )
                    .join(
                        InvestigationRunV2Record,
                        InvestigationRunV2Record.id
                        == InvestigationActivityV2Record.investigation_id,
                    )
                    .where(
                        InvestigationRunV2Record.created_at >= stored_start,
                        InvestigationRunV2Record.created_at < stored_end,
                        InvestigationActivityV2Record.kind == "tool_call_completed",
                    )
                    .group_by(InvestigationActivityV2Record.investigation_id)
                )
            }
            latest_feedback: dict[str, tuple[str, int]] = {}
            for investigation_id, rating, sequence in session.execute(
                select(
                    InvestigationFeedbackV2Record.investigation_id,
                    InvestigationFeedbackV2Record.rating,
                    InvestigationFeedbackV2Record.sequence,
                )
                .join(
                    InvestigationRunV2Record,
                    InvestigationRunV2Record.id
                    == InvestigationFeedbackV2Record.investigation_id,
                )
                .where(
                    InvestigationRunV2Record.created_at >= stored_start,
                    InvestigationRunV2Record.created_at < stored_end,
                )
                .order_by(
                    InvestigationFeedbackV2Record.investigation_id,
                    InvestigationFeedbackV2Record.sequence,
                )
            ):
                latest_feedback[str(investigation_id)] = (str(rating), int(sequence))
            run_rows = session.execute(
                select(
                    InvestigationRunV2Record,
                    OperationalOccurrenceRecord.source_id,
                    OperationalOccurrenceRecord.service_id,
                    OperationalOccurrenceRecord.signal_severity,
                    EvidenceSnapshotV2Record.created_at,
                    InvestigationReportV2Record.created_at,
                    InvestigationReportV2Record.report_json,
                )
                .join(
                    OperationalOccurrenceRecord,
                    OperationalOccurrenceRecord.id == InvestigationRunV2Record.occurrence_id,
                )
                .outerjoin(
                    EvidenceSnapshotV2Record,
                    EvidenceSnapshotV2Record.investigation_id == InvestigationRunV2Record.id,
                )
                .outerjoin(
                    InvestigationReportV2Record,
                    InvestigationReportV2Record.investigation_id == InvestigationRunV2Record.id,
                )
                .where(
                    InvestigationRunV2Record.created_at >= stored_start,
                    InvestigationRunV2Record.created_at < stored_end,
                )
            )
            for run, source_id, service_id, severity, snapshot_at, report_at, report_json in run_rows:
                dimension_values = (
                    str(source_id),
                    _UNMAPPED if service_id is None else str(service_id),
                    str(severity),
                )
                created = _aware(run.created_at)
                hour = created.replace(minute=0, second=0, microsecond=0)
                dimension = (hour.date().isoformat(), *dimension_values)
                hours = buckets.setdefault(dimension, {})
                hour_key = hour.isoformat().replace("+00:00", "Z")
                counter = cast(dict[str, Any], hours.setdefault(hour_key, _counter()))
                mode = run.model_execution_mode
                if run.status == "COMPLETED" and report_at is not None:
                    terminal = "p2_valid"
                elif run.status in {"COMPLETED", "DEGRADED"}:
                    terminal = "evidence_only"
                elif run.status == "CANCELED":
                    terminal = "canceled"
                elif run.status == "FAILED" and "CONTRACT" in (run.safe_error_code or ""):
                    terminal = "contract_rejected"
                elif run.status == "FAILED":
                    terminal = "dependency_failed"
                else:
                    terminal = "pending"
                _increment_mode(counter, "ai", mode, terminal)
                if run.request_count > 0:
                    _increment_mode(counter, "ai", mode, "model_started")
                    if mode != "FAKE":
                        _increment_mode(counter, "ai", mode, "unknown_cost")
                feedback = latest_feedback.get(run.id)
                eligible_feedback = False
                if report_json:
                    raw_report = json.loads(report_json)
                    eligible_feedback = bool(
                        isinstance(raw_report, dict)
                        and raw_report.get("recommended_actions")
                    )
                if eligible_feedback:
                    _increment_mode(counter, "ai", mode, "feedback_eligible")
                if feedback is not None:
                    _increment_mode(counter, "ai", mode, "feedback_response")
                    if feedback[0] == "ADOPTED":
                        _increment_mode(counter, "ai", mode, "feedback_adopted")
                stage_times = (
                    ("p0_mtti", snapshot_at),
                    ("p1_mtti", first_tool_at.get(run.id)),
                    ("p2_mtti", report_at),
                )
                for metric, stage_at in stage_times:
                    if stage_at is None:
                        continue
                    samples.append(
                        AnalyticsDurationSampleRecord(
                            fact_key=f"{metric}:{run.id}",
                            day_utc=hour.date().isoformat(),
                            source_id=dimension_values[0],
                            service_key=dimension_values[1],
                            signal_severity=dimension_values[2],
                            metric=f"{metric}:{mode}",
                            value_ms=_milliseconds(created, _aware(stage_at)),
                            occurred_at=_stored(created),
                        )
                    )

            for fact in session.scalars(
                select(NoiseLifecycleFactRecord).where(
                    NoiseLifecycleFactRecord.occurred_at >= stored_start,
                    NoiseLifecycleFactRecord.occurred_at < stored_end,
                    NoiseLifecycleFactRecord.transition == "ACTIVATED",
                )
            ):
                occurred = _aware(fact.occurred_at)
                hour = occurred.replace(minute=0, second=0, microsecond=0)
                dimension = (
                    hour.date().isoformat(),
                    fact.source_id,
                    fact.service_key,
                    fact.signal_severity,
                )
                hours = buckets.setdefault(dimension, {})
                hour_key = hour.isoformat().replace("+00:00", "Z")
                counter = cast(dict[str, Any], hours.setdefault(hour_key, _counter()))
                key = (
                    "flapping_activations"
                    if fact.kind == "FLAPPING"
                    else "storm_activations"
                )
                counter[key] += 1

            first_day = start.date().isoformat()
            last_day = end.date().isoformat()
            session.execute(
                delete(AnalyticsDailyBucketRecord).where(
                    AnalyticsDailyBucketRecord.day_utc >= first_day,
                    AnalyticsDailyBucketRecord.day_utc <= last_day,
                )
            )
            session.execute(
                delete(AnalyticsDurationSampleRecord).where(
                    AnalyticsDurationSampleRecord.day_utc >= first_day,
                    AnalyticsDurationSampleRecord.day_utc <= last_day,
                )
            )
            refreshed_at = _stored(now)
            for (day, source, service, severity), hours in buckets.items():
                session.add(
                    AnalyticsDailyBucketRecord(
                        day_utc=day,
                        source_id=source,
                        service_key=service,
                        signal_severity=severity,
                        hours_json=json.dumps(
                            hours, sort_keys=True, separators=(",", ":")
                        ),
                        updated_at=refreshed_at,
                    )
                )
            session.add_all(samples)
            state = session.get(AnalyticsRollupStateRecord, 1)
            if state is None:
                session.add(
                    AnalyticsRollupStateRecord(
                        id=1,
                        last_complete_hour=stored_end,
                        refreshed_at=refreshed_at,
                    )
                )
            else:
                state.last_complete_hour = stored_end
                state.refreshed_at = refreshed_at

        return AnalyticsRefreshResult(len(buckets), len(samples), _aware(refreshed_at))

    def cleanup(self, *, now: datetime, retention_days: int = 365) -> tuple[int, int]:
        """Delete only persisted projection rows older than the fixed horizon."""

        cutoff_day = (now.astimezone(UTC) - timedelta(days=retention_days)).date()
        with self._sessions.begin() as session:
            buckets = cast(
                Any,
                session.execute(
                    delete(AnalyticsDailyBucketRecord).where(
                        AnalyticsDailyBucketRecord.day_utc < cutoff_day.isoformat()
                    )
                ),
            ).rowcount
            samples = cast(
                Any,
                session.execute(
                    delete(AnalyticsDurationSampleRecord).where(
                        AnalyticsDurationSampleRecord.day_utc < cutoff_day.isoformat()
                    )
                ),
            ).rowcount
        return int(buckets or 0), int(samples or 0)

    def overview(
        self,
        *,
        range_value: str = "7d",
        source_id: str | None = None,
        service_id: int | None = None,
        signal_severity: str | None = None,
        now: datetime,
    ) -> AnalyticsOverview:
        window = analytics_window(range_value, now=now)
        counters: dict[str, Any] = _counter()
        metric_values: dict[str, list[int]] = defaultdict(list)
        generated_at: datetime | None = None
        with self._sessions() as session:
            state = session.get(AnalyticsRollupStateRecord, 1)
            if state is not None:
                generated_at = _aware(state.refreshed_at)
            statement = select(AnalyticsDailyBucketRecord).where(
                AnalyticsDailyBucketRecord.day_utc >= window.start.date().isoformat(),
                AnalyticsDailyBucketRecord.day_utc <= window.end.date().isoformat(),
            )
            sample_statement = select(AnalyticsDurationSampleRecord).where(
                AnalyticsDurationSampleRecord.occurred_at >= _stored(window.start),
                AnalyticsDurationSampleRecord.occurred_at < _stored(window.end),
            )
            if source_id is not None:
                statement = statement.where(AnalyticsDailyBucketRecord.source_id == source_id)
                sample_statement = sample_statement.where(
                    AnalyticsDurationSampleRecord.source_id == source_id
                )
            if service_id is not None:
                service_key = str(service_id)
                statement = statement.where(AnalyticsDailyBucketRecord.service_key == service_key)
                sample_statement = sample_statement.where(
                    AnalyticsDurationSampleRecord.service_key == service_key
                )
            if signal_severity is not None:
                statement = statement.where(
                    AnalyticsDailyBucketRecord.signal_severity == signal_severity
                )
                sample_statement = sample_statement.where(
                    AnalyticsDurationSampleRecord.signal_severity == signal_severity
                )
            for row in session.scalars(statement):
                raw = json.loads(row.hours_json)
                if not isinstance(raw, dict):
                    continue
                for hour_key, hour_value in raw.items():
                    hour = datetime.fromisoformat(str(hour_key).replace("Z", "+00:00"))
                    if not window.start <= hour < window.end or not isinstance(hour_value, dict):
                        continue
                    for key in (
                        "new_occurrences",
                        "new_alert_instances",
                        "creation_known",
                        "creation_unmapped",
                        "creation_unknown",
                        "current_unmapped",
                        "ack_sla_eligible",
                        "ack_sla_breached",
                        "flapping_activations",
                        "storm_activations",
                    ):
                        counters[key] += int(hour_value.get(key, 0))
                    for key in ("tasks", "resolution_codes"):
                        nested = hour_value.get(key, {})
                        if isinstance(nested, dict):
                            for name, count in nested.items():
                                target = cast(dict[str, int], counters[key])
                                target[str(name)] = target.get(str(name), 0) + int(count)
                    for section in ("notifications", "ai"):
                        groups = hour_value.get(section, {})
                        if not isinstance(groups, dict):
                            continue
                        for mode, values in groups.items():
                            if not isinstance(values, dict):
                                continue
                            target_group = _mode_counter(counters, section, str(mode))
                            for name, count in values.items():
                                target_group[str(name)] = (
                                    target_group.get(str(name), 0) + int(count)
                                )
            for sample in session.scalars(sample_statement):
                metric_values[sample.metric].append(sample.value_ms)

        occurrences = int(counters["new_occurrences"])
        alerts = int(counters["new_alert_instances"])
        notification_views: dict[str, NotificationAnalyticsView] = {}
        ai_views: dict[str, AiAnalyticsView] = {}
        notification_groups = cast(
            dict[str, dict[str, int]], counters["notifications"]
        )
        ai_groups = cast(dict[str, dict[str, int]], counters["ai"])
        for mode in _EXECUTION_MODES:
            values = notification_groups.get(mode, {})
            succeeded = values.get("succeeded", 0)
            failed = values.get("permanently_failed", 0)
            notification_views[mode] = NotificationAnalyticsView(
                succeeded=succeeded,
                permanently_failed=failed,
                pending_backlog=values.get("pending_backlog", 0),
                noise=values.get("noise", 0),
                success_rate=ratio_summary(succeeded, succeeded + failed),
                latency=duration_summary(metric_values[f"delivery_latency:{mode}"]),
            )
            ai_values = ai_groups.get(mode, {})
            started = ai_values.get("model_started", 0)
            p2_valid = ai_values.get("p2_valid", 0)
            ai_views[mode] = AiAnalyticsView(
                p2_valid=p2_valid,
                evidence_only=ai_values.get("evidence_only", 0),
                canceled=ai_values.get("canceled", 0),
                contract_rejected=ai_values.get("contract_rejected", 0),
                dependency_failed=ai_values.get("dependency_failed", 0),
                pending=ai_values.get("pending", 0),
                model_started=started,
                unknown_cost=ai_values.get("unknown_cost", 0),
                p2_success=ratio_summary(p2_valid, started),
                feedback_response=ratio_summary(
                    ai_values.get("feedback_response", 0),
                    ai_values.get("feedback_eligible", 0),
                ),
                feedback_adoption=ratio_summary(
                    ai_values.get("feedback_adopted", 0),
                    ai_values.get("feedback_response", 0),
                ),
                p0_mtti=duration_summary(metric_values[f"p0_mtti:{mode}"]),
                p1_mtti=duration_summary(metric_values[f"p1_mtti:{mode}"]),
                p2_mtti=duration_summary(metric_values[f"p2_mtti:{mode}"]),
            )
        return AnalyticsOverview(
            freshness="ROLLUP_PENDING" if generated_at is None else "READY",
            generated_at=generated_at,
            window=window,
            response=ResponseAnalyticsView(
                mtta=DurationMetricView(
                    duration_summary(metric_values["mtta_completed"]),
                    duration_summary(metric_values["mtta_unfinished"]),
                ),
                resolution=DurationMetricView(
                    duration_summary(metric_values["resolution_completed"]),
                    duration_summary(metric_values["resolution_unfinished"]),
                ),
                task_outcomes=dict(cast(dict[str, int], counters["tasks"])),
                resolution_codes=dict(
                    cast(dict[str, int], counters["resolution_codes"])
                ),
            ),
            signal=SignalAnalyticsView(
                new_occurrences=occurrences,
                new_alert_instances=alerts,
                compression=ratio_summary(max(0, alerts - occurrences), alerts),
                alerts_per_occurrence=rate_summary(alerts, occurrences),
                creation_unmapped=ratio_summary(
                    int(counters["creation_unmapped"]), int(counters["creation_known"])
                ),
                creation_assignment_unknown=int(counters["creation_unknown"]),
                current_unmapped=int(counters["current_unmapped"]),
                ack_sla_breach=ratio_summary(
                    int(counters["ack_sla_breached"]), int(counters["ack_sla_eligible"])
                ),
                flapping_activations=int(counters["flapping_activations"]),
                storm_activations=int(counters["storm_activations"]),
            ),
            notifications=notification_views,
            ai=ai_views,
        )
