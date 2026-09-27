from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.application.retention import RetentionBatchResult, RetentionPolicy
from app.platform.persistence.codecs import stored_utc_text as _stored


class AnalyticsRollupStore(Protocol):
    def refresh(self, *, now: datetime, rebuild_days: int) -> object: ...


class InvestigationRollupStore(Protocol):
    def finalize_retention_rollups(
        self,
        *,
        now: datetime,
        raw_retention_days: int,
    ) -> object: ...


SessionFactory = Callable[[], Session]


class SqlAlchemyRetentionPersistence:
    """Bounded, re-entrant retention for the operations schema.

    The application layer owns policy and pass orchestration. This adapter owns
    the concrete schema knowledge, including the additive gates that permit the
    retention worker (and only that worker) to remove append-only records.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        analytics: AnalyticsRollupStore,
        investigations: InvestigationRollupStore,
    ) -> None:
        self._session_factory = session_factory
        self._analytics = analytics
        self._investigations = investigations

    def prepare_rollups(self, *, as_of: datetime, policy: RetentionPolicy) -> None:
        self._analytics.refresh(
            now=as_of,
            rebuild_days=policy.investigation_days + 1,
        )
        self._investigations.finalize_retention_rollups(
            now=as_of,
            raw_retention_days=policy.investigation_days,
        )

    def cleanup_batch(
        self,
        *,
        as_of: datetime,
        policy: RetentionPolicy,
    ) -> RetentionBatchResult:
        cutoffs = {
            "invalid": _stored(as_of - timedelta(days=policy.invalid_raw_days)),
            "runtime": _stored(as_of - timedelta(days=policy.runtime_days)),
            "investigation": _stored(
                as_of - timedelta(days=policy.investigation_days)
            ),
            "poll": _stored(as_of - timedelta(days=policy.poll_detail_days)),
            "history": _stored(as_of - timedelta(days=policy.history_days)),
            "as_of": _stored(as_of),
            "history_day": (as_of - timedelta(days=policy.history_days))
            .astimezone(UTC)
            .date()
            .isoformat(),
        }
        deleted: dict[str, int] = {}
        has_more = False

        groups = (
            self._cleanup_investigations,
            self._cleanup_jobs_and_events,
            self._cleanup_poll_details,
            self._cleanup_runtime,
            self._cleanup_history,
        )
        for cleanup in groups:
            with self._session_factory() as session:
                group_deleted, group_more = cleanup(
                    session,
                    cutoffs=cutoffs,
                    policy=policy,
                )
                session.commit()
            for table, count in group_deleted.items():
                deleted[table] = deleted.get(table, 0) + count
            has_more = has_more or group_more

        return RetentionBatchResult(deleted_by_table=deleted, has_more=has_more)

    @staticmethod
    def _record(target: dict[str, int], table: str, count: int) -> None:
        if count:
            target[table] = target.get(table, 0) + count

    @staticmethod
    def _delete_selected(
        session: Session,
        *,
        table: str,
        key: str,
        select_sql: str,
        params: dict[str, object],
        limit: int,
    ) -> tuple[int, bool]:
        rows = session.execute(
            text(select_sql),
            {**params, "probe_limit": limit + 1},
        ).scalars().all()
        selected = list(rows[:limit])
        if selected:
            statement = text(f"DELETE FROM {table} WHERE {key} IN :retention_keys").bindparams(
                bindparam("retention_keys", expanding=True)
            )
            session.execute(statement, {"retention_keys": selected})
        return len(selected), len(rows) > limit

    def _cleanup_investigations(
        self,
        session: Session,
        *,
        cutoffs: Mapping[str, object],
        policy: RetentionPolicy,
    ) -> tuple[dict[str, int], bool]:
        deleted: dict[str, int] = {}
        has_more = False
        limit = policy.batch_rows
        session.execute(
            text("UPDATE investigation_retention_gate SET enabled = 1 WHERE id = 1")
        )

        count, more = self._delete_selected(
            session,
            table="invalid_analyst_response",
            key="investigation_id",
            select_sql="""
                SELECT investigation_id FROM invalid_analyst_response
                WHERE purge_after <= :as_of
                ORDER BY purge_after, investigation_id
                LIMIT :probe_limit
            """,
            params={"as_of": cutoffs["as_of"]},
            limit=limit,
        )
        self._record(deleted, "invalid_analyst_response", count)
        has_more = has_more or more

        legacy_parent = """
            SELECT id FROM investigation
            WHERE status IN ('EVIDENCE_ONLY', 'FAILED')
              AND updated_at < :cutoff
        """
        for table in (
            "investigation_feedback_v1",
            "invalid_analyst_response",
            "analyst_result_v1",
            "investigation_canceled_v1",
            "planner_label_rejection_v1",
            "planner_step_v1",
            "investigation_degradation_v1",
            "metric_empty_observation_v1",
            "metric_observation_v1",
            "similar_history_observation_v1",
            "evidence_brief_v1",
            "investigation_phase_transition_v1",
            "initial_investigation_snapshot",
            "investigation_trajectory_head",
        ):
            count, more = self._delete_selected(
                session,
                table=table,
                key="rowid",
                select_sql=f"""
                    SELECT rowid FROM {table}
                    WHERE investigation_id IN ({legacy_parent})
                    ORDER BY rowid
                    LIMIT :probe_limit
                """,
                params={"cutoff": cutoffs["investigation"]},
                limit=limit,
            )
            self._record(deleted, table, count)
            has_more = has_more or more

        legacy_children = (
            "investigation_feedback_v1",
            "invalid_analyst_response",
            "analyst_result_v1",
            "investigation_canceled_v1",
            "planner_label_rejection_v1",
            "planner_step_v1",
            "investigation_degradation_v1",
            "metric_empty_observation_v1",
            "metric_observation_v1",
            "similar_history_observation_v1",
            "evidence_brief_v1",
            "investigation_phase_transition_v1",
            "initial_investigation_snapshot",
            "investigation_trajectory_head",
        )
        legacy_not_exists = " AND ".join(
            f"NOT EXISTS (SELECT 1 FROM {table} child WHERE child.investigation_id = investigation.id)"
            for table in legacy_children
        )
        count, more = self._delete_selected(
            session,
            table="investigation",
            key="id",
            select_sql=f"""
                SELECT id FROM investigation
                WHERE status IN ('EVIDENCE_ONLY', 'FAILED')
                  AND updated_at < :cutoff
                  AND {legacy_not_exists}
                ORDER BY updated_at, id
                LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["investigation"]},
            limit=limit,
        )
        self._record(deleted, "investigation", count)
        has_more = has_more or more

        v2_parent = """
            SELECT id FROM investigation_run_v2
            WHERE status IN ('COMPLETED', 'DEGRADED', 'FAILED', 'CANCELED')
              AND updated_at < :cutoff
        """
        v2_children = (
            "investigation_feedback_v2",
            "investigation_metric_observation_v2",
            "investigation_activity_v2",
            "investigation_report_v2",
            "investigation_tool_scope_v2",
            "evidence_snapshot_v2",
        )
        for table in v2_children:
            count, more = self._delete_selected(
                session,
                table=table,
                key="rowid",
                select_sql=f"""
                    SELECT rowid FROM {table}
                    WHERE investigation_id IN ({v2_parent})
                    ORDER BY rowid
                    LIMIT :probe_limit
                """,
                params={"cutoff": cutoffs["investigation"]},
                limit=limit,
            )
            self._record(deleted, table, count)
            has_more = has_more or more
        v2_not_exists = " AND ".join(
            f"NOT EXISTS (SELECT 1 FROM {table} child WHERE child.investigation_id = investigation_run_v2.id)"
            for table in v2_children
        )
        count, more = self._delete_selected(
            session,
            table="investigation_run_v2",
            key="id",
            select_sql=f"""
                SELECT id FROM investigation_run_v2
                WHERE status IN ('COMPLETED', 'DEGRADED', 'FAILED', 'CANCELED')
                  AND updated_at < :cutoff
                  AND {v2_not_exists}
                ORDER BY updated_at, id
                LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["investigation"]},
            limit=limit,
        )
        self._record(deleted, "investigation_run_v2", count)
        has_more = has_more or more

        count, more = self._delete_selected(
            session,
            table="investigation_daily_usage_v1",
            key="day_utc",
            select_sql="""
                SELECT day_utc FROM investigation_daily_usage_v1
                WHERE day_utc < :cutoff_day
                ORDER BY day_utc
                LIMIT :probe_limit
            """,
            params={"cutoff_day": cutoffs["history_day"]},
            limit=limit,
        )
        self._record(deleted, "investigation_daily_usage_v1", count)
        has_more = has_more or more
        session.execute(
            text("UPDATE investigation_retention_gate SET enabled = 0 WHERE id = 1")
        )
        return deleted, has_more

    def _cleanup_jobs_and_events(
        self,
        session: Session,
        *,
        cutoffs: Mapping[str, object],
        policy: RetentionPolicy,
    ) -> tuple[dict[str, int], bool]:
        deleted: dict[str, int] = {}
        has_more = False
        terminal_job = "('SUCCEEDED', 'FAILED', 'CANCELED')"
        job_parent = f"""
            SELECT id FROM platform_job
            WHERE state IN {terminal_job}
              AND COALESCE(finished_at, updated_at, created_at) < :cutoff
        """
        count, more = self._delete_selected(
            session,
            table="platform_event",
            key="sequence",
            select_sql=f"""
                SELECT sequence FROM platform_event
                WHERE subject_type = 'job'
                  AND subject_id IN ({job_parent})
                ORDER BY sequence
                LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["runtime"]},
            limit=policy.batch_rows,
        )
        self._record(deleted, "platform_event", count)
        has_more = has_more or more

        count, more = self._delete_selected(
            session,
            table="platform_job",
            key="id",
            select_sql=f"""
                SELECT id FROM platform_job
                WHERE state IN {terminal_job}
                  AND COALESCE(finished_at, updated_at, created_at) < :cutoff
                  AND NOT EXISTS (
                    SELECT 1 FROM platform_event event
                    WHERE event.subject_type = 'job'
                      AND event.subject_id = platform_job.id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM investigation run
                    WHERE run.job_id = platform_job.id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM investigation_run_v2 run
                    WHERE run.job_id = platform_job.id
                  )
                ORDER BY COALESCE(finished_at, updated_at, created_at), id
                LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["runtime"]},
            limit=policy.batch_rows,
        )
        self._record(deleted, "platform_job", count)
        has_more = has_more or more

        count, more = self._delete_selected(
            session,
            table="platform_event",
            key="sequence",
            select_sql="""
                SELECT sequence FROM platform_event
                WHERE subject_type <> 'job'
                  AND created_at < :cutoff
                ORDER BY created_at, sequence
                LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["history"]},
            limit=policy.batch_rows,
        )
        self._record(deleted, "platform_event", count)
        has_more = has_more or more
        return deleted, has_more

    def _cleanup_poll_details(
        self,
        session: Session,
        *,
        cutoffs: Mapping[str, object],
        policy: RetentionPolicy,
    ) -> tuple[dict[str, int], bool]:
        deleted: dict[str, int] = {}
        has_more = False
        eligible_runs = """
            SELECT id FROM (
                SELECT id, started_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_id
                           ORDER BY started_at DESC, id DESC
                       ) AS source_rank
                FROM source_poll_run
            ) ranked
            WHERE started_at < :cutoff
              AND source_rank > :keep_runs
        """
        params: dict[str, object] = {
            "cutoff": cutoffs["poll"],
            "keep_runs": policy.poll_runs_per_source,
        }
        count, more = self._delete_selected(
            session,
            table="endpoint_poll_result",
            key="id",
            select_sql=f"""
                SELECT id FROM endpoint_poll_result
                WHERE poll_run_id IN ({eligible_runs})
                ORDER BY id
                LIMIT :probe_limit
            """,
            params=params,
            limit=policy.batch_rows,
        )
        self._record(deleted, "endpoint_poll_result", count)
        has_more = has_more or more
        count, more = self._delete_selected(
            session,
            table="source_poll_run",
            key="id",
            select_sql=f"""
                SELECT id FROM source_poll_run
                WHERE id IN ({eligible_runs})
                  AND NOT EXISTS (
                    SELECT 1 FROM endpoint_poll_result result
                    WHERE result.poll_run_id = source_poll_run.id
                  )
                ORDER BY started_at, id
                LIMIT :probe_limit
            """,
            params=params,
            limit=policy.batch_rows,
        )
        self._record(deleted, "source_poll_run", count)
        has_more = has_more or more
        return deleted, has_more

    def _cleanup_runtime(
        self,
        session: Session,
        *,
        cutoffs: Mapping[str, object],
        policy: RetentionPolicy,
    ) -> tuple[dict[str, int], bool]:
        deleted: dict[str, int] = {}
        has_more = False
        limit = policy.batch_rows

        statements = (
            (
                "metric_backfill_run",
                "id",
                """SELECT id FROM metric_backfill_run
                   WHERE completed_at IS NOT NULL AND completed_at < :cutoff
                   ORDER BY completed_at, id LIMIT :probe_limit""",
            ),
            (
                "notification_attempt",
                "id",
                """SELECT attempt.id FROM notification_attempt attempt
                   JOIN notification_delivery delivery ON delivery.id = attempt.delivery_id
                   WHERE delivery.state IN ('SUCCEEDED', 'PERMANENTLY_FAILED', 'SUPPRESSED', 'CANCELED')
                     AND delivery.updated_at < :cutoff
                   ORDER BY attempt.id LIMIT :probe_limit""",
            ),
            (
                "notification_delivery",
                "id",
                """SELECT id FROM notification_delivery
                   WHERE state IN ('SUCCEEDED', 'PERMANENTLY_FAILED', 'SUPPRESSED', 'CANCELED')
                     AND updated_at < :cutoff
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_attempt attempt
                       WHERE attempt.delivery_id = notification_delivery.id
                     )
                   ORDER BY updated_at, id LIMIT :probe_limit""",
            ),
            (
                "notification_route_target",
                "id",
                """SELECT target.id FROM notification_route_target target
                   JOIN notification_route route ON route.id = target.route_id
                   WHERE route.status = 'TERMINATED'
                     AND route.closed_at < :cutoff
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_delivery delivery
                       WHERE delivery.route_target_id = target.id
                     )
                   ORDER BY target.id LIMIT :probe_limit""",
            ),
            (
                "notification_route",
                "id",
                """SELECT id FROM notification_route
                   WHERE status = 'TERMINATED' AND closed_at < :cutoff
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_route_target target
                       WHERE target.route_id = notification_route.id
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM notification_delivery delivery
                       WHERE delivery.route_id = notification_route.id
                     )
                   ORDER BY closed_at, id LIMIT :probe_limit""",
            ),
            (
                "storm_notification_summary",
                "summary_key",
                """SELECT summary_key FROM storm_notification_summary
                   WHERE created_at < :cutoff
                   ORDER BY created_at, summary_key LIMIT :probe_limit""",
            ),
            (
                "alert",
                "id",
                """SELECT id FROM alert
                   WHERE source_state = 'RECOVERED' AND last_seen_at < :cutoff
                   ORDER BY last_seen_at, id LIMIT :probe_limit""",
            ),
        )
        for table, key, select_sql in statements:
            count, more = self._delete_selected(
                session,
                table=table,
                key=key,
                select_sql=select_sql,
                params={"cutoff": cutoffs["runtime"]},
                limit=limit,
            )
            self._record(deleted, table, count)
            has_more = has_more or more
        return deleted, has_more

    def _cleanup_history(
        self,
        session: Session,
        *,
        cutoffs: Mapping[str, object],
        policy: RetentionPolicy,
    ) -> tuple[dict[str, int], bool]:
        deleted: dict[str, int] = {}
        has_more = False
        limit = policy.batch_rows
        old_occurrence = """
            SELECT id FROM operational_occurrence
            WHERE response_state = 'RESOLVED'
              AND resolved_at IS NOT NULL
              AND resolved_at < :cutoff
        """
        for table in ("incident_note_content", "occurrence_suppression"):
            count, more = self._delete_selected(
                session,
                table=table,
                key="id",
                select_sql=f"""
                    SELECT id FROM {table}
                    WHERE occurrence_id IN ({old_occurrence})
                    ORDER BY id LIMIT :probe_limit
                """,
                params={"cutoff": cutoffs["history"]},
                limit=limit,
            )
            self._record(deleted, table, count)
            has_more = has_more or more

        count, more = self._delete_selected(
            session,
            table="incident_task",
            key="id",
            select_sql=f"""
                SELECT id FROM incident_task
                WHERE occurrence_id IN ({old_occurrence})
                  AND status IN ('DONE', 'CANCELED')
                ORDER BY id LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["history"]},
            limit=limit,
        )
        self._record(deleted, "incident_task", count)
        has_more = has_more or more

        session.execute(text("UPDATE operations_retention_gate SET enabled = 1 WHERE id = 1"))
        count, more = self._delete_selected(
            session,
            table="incident_timeline_entry",
            key="id",
            select_sql=f"""
                SELECT id FROM incident_timeline_entry
                WHERE occurrence_id IN ({old_occurrence})
                ORDER BY id LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["history"]},
            limit=limit,
        )
        self._record(deleted, "incident_timeline_entry", count)
        has_more = has_more or more
        session.execute(text("UPDATE operations_retention_gate SET enabled = 0 WHERE id = 1"))

        count, more = self._delete_selected(
            session,
            table="operational_occurrence",
            key="id",
            select_sql="""
                SELECT id FROM operational_occurrence
                WHERE response_state = 'RESOLVED'
                  AND resolved_at IS NOT NULL
                  AND resolved_at < :cutoff
                  AND NOT EXISTS (SELECT 1 FROM incident_task child WHERE child.occurrence_id = operational_occurrence.id)
                  AND NOT EXISTS (SELECT 1 FROM incident_note_content child WHERE child.occurrence_id = operational_occurrence.id)
                  AND NOT EXISTS (SELECT 1 FROM occurrence_suppression child WHERE child.occurrence_id = operational_occurrence.id)
                  AND NOT EXISTS (SELECT 1 FROM incident_timeline_entry child WHERE child.occurrence_id = operational_occurrence.id)
                  AND NOT EXISTS (SELECT 1 FROM investigation child WHERE child.occurrence_id = operational_occurrence.id)
                  AND NOT EXISTS (SELECT 1 FROM investigation_run_v2 child WHERE child.occurrence_id = operational_occurrence.id)
                  AND NOT EXISTS (SELECT 1 FROM operational_occurrence duplicate WHERE duplicate.duplicate_of_occurrence_id = operational_occurrence.id)
                  AND NOT EXISTS (SELECT 1 FROM notification_route route WHERE route.incident_id = operational_occurrence.incident_id AND route.occurrence_no = operational_occurrence.occurrence_no)
                ORDER BY resolved_at, id LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["history"]},
            limit=limit,
        )
        self._record(deleted, "operational_occurrence", count)
        has_more = has_more or more

        statements = (
            (
                "incident_occurrence",
                "id",
                """SELECT id FROM incident_occurrence
                   WHERE recovered_at IS NOT NULL AND recovered_at < :cutoff
                   ORDER BY recovered_at, id LIMIT :probe_limit""",
            ),
            (
                "incident_audit",
                "id",
                """SELECT id FROM incident_audit
                   WHERE created_at < :cutoff
                   ORDER BY created_at, id LIMIT :probe_limit""",
            ),
            (
                "source_audit",
                "sequence",
                """SELECT sequence FROM source_audit WHERE changed_at < :cutoff
                   ORDER BY changed_at, sequence LIMIT :probe_limit""",
            ),
            (
                "model_channel_audit",
                "sequence",
                """SELECT sequence FROM model_channel_audit WHERE changed_at < :cutoff
                   ORDER BY changed_at, sequence LIMIT :probe_limit""",
            ),
            (
                "notification_channel_audit",
                "sequence",
                """SELECT sequence FROM notification_channel_audit WHERE changed_at < :cutoff
                   ORDER BY changed_at, sequence LIMIT :probe_limit""",
            ),
            (
                "notification_policy_audit",
                "sequence",
                """SELECT sequence FROM notification_policy_audit WHERE changed_at < :cutoff
                   ORDER BY changed_at, sequence LIMIT :probe_limit""",
            ),
            (
                "service_audit",
                "id",
                """SELECT id FROM service_audit WHERE changed_at < :cutoff
                   ORDER BY changed_at, id LIMIT :probe_limit""",
            ),
            (
                "noise_lifecycle_fact",
                "id",
                """SELECT id FROM noise_lifecycle_fact WHERE occurred_at < :cutoff
                   ORDER BY occurred_at, id LIMIT :probe_limit""",
            ),
            (
                "maintenance_window",
                "id",
                """SELECT id FROM maintenance_window
                   WHERE status = 'ENDED' AND ended_at IS NOT NULL AND ended_at < :cutoff
                   ORDER BY ended_at, id LIMIT :probe_limit""",
            ),
            (
                "analytics_duration_sample",
                "rowid",
                """SELECT rowid FROM analytics_duration_sample WHERE day_utc < :cutoff_day
                   ORDER BY day_utc, rowid LIMIT :probe_limit""",
            ),
            (
                "analytics_daily_bucket",
                "rowid",
                """SELECT rowid FROM analytics_daily_bucket WHERE day_utc < :cutoff_day
                   ORDER BY day_utc, rowid LIMIT :probe_limit""",
            ),
        )
        params = {
            "cutoff": cutoffs["history"],
            "cutoff_day": cutoffs["history_day"],
        }
        for table, key, select_sql in statements:
            count, more = self._delete_selected(
                session,
                table=table,
                key=key,
                select_sql=select_sql,
                params=params,
                limit=limit,
            )
            self._record(deleted, table, count)
            has_more = has_more or more

        count, more = self._delete_selected(
            session,
            table="incident",
            key="id",
            select_sql="""
                SELECT id FROM incident
                WHERE source_state = 'RECOVERED' AND updated_at < :cutoff
                  AND NOT EXISTS (SELECT 1 FROM alert child WHERE child.incident_id = incident.id)
                  AND NOT EXISTS (SELECT 1 FROM incident_audit child WHERE child.incident_id = incident.id)
                  AND NOT EXISTS (SELECT 1 FROM incident_occurrence child WHERE child.incident_id = incident.id)
                  AND NOT EXISTS (SELECT 1 FROM operational_occurrence child WHERE child.incident_id = incident.id)
                  AND NOT EXISTS (SELECT 1 FROM notification_route child WHERE child.incident_id = incident.id)
                ORDER BY updated_at, id LIMIT :probe_limit
            """,
            params={"cutoff": cutoffs["history"]},
            limit=limit,
        )
        self._record(deleted, "incident", count)
        has_more = has_more or more
        return deleted, has_more
