"""Explicit platform composition over a caller-selected database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from fastapi import FastAPI
from sqlalchemy import Engine

from app.adapters.monitoring.alertmanager import BoundedAlertmanagerReader
from app.adapters.monitoring.grafana import BoundedGrafanaReader
from app.adapters.monitoring.thanos import BoundedThanosReader
from app.adapters.models.investigator import (
    PydanticInvestigatorProbe,
    PydanticInvestigatorRuntime,
    offline_investigator_runtime,
)
from app.adapters.notifications.providers import NotificationProviderRegistry
from app.adapters.persistence.jobs import JobRepository, SqlAlchemyJobStore
from app.adapters.persistence.analytics import SqlAlchemyAnalyticsStore
from app.adapters.persistence.catalog import SqlAlchemyServiceCatalogStore
from app.adapters.persistence.commands import SqlAlchemyCommandReceiptStore
from app.adapters.persistence.incidents import (
    SqlAlchemyIncidentStore,
    occurrence_response_state_in_session,
)
from app.adapters.persistence.investigations import SqlAlchemyInvestigationStore
from app.adapters.persistence.unified_investigations import (
    SqlAlchemyUnifiedInvestigationStore,
)
from app.application.incidents import (
    IncidentCollaborationCommands,
    IncidentResponseCommands,
    IncidentServiceAssignmentCommands,
)
from app.application.investigations import StartInvestigation
from app.application.unified_investigations import (
    RunUnifiedInvestigation,
    StartUnifiedInvestigation,
)
from app.application.evidence_expansion import (
    PlannerModelCallError,
    PlannerModelTarget,
    RunEvidenceExpansion,
)
from app.adapters.persistence.notifications import SqlAlchemyNotificationStore
from app.adapters.persistence.notification_enrichment import (
    read_notification_enrichment_in_session,
)
from app.adapters.persistence.noise import SqlAlchemyNoiseStore
from app.adapters.persistence.observability import SqlAlchemyObservabilityStore
from app.adapters.persistence.platform_health import SqlAlchemyPlatformHealthReader
from app.adapters.persistence.retention import SqlAlchemyRetentionPersistence
from app.adapters.persistence.sources import SqlAlchemySourceStore
from app.api.v1.router import create_api_v1_router
from app.api.v1.sources import create_sources_router
from app.api.v1.observability import create_observability_router
from app.api.v1.incidents import create_incidents_router
from app.api.v1.investigations import create_investigations_router
from app.api.v1.unified_investigations import create_unified_investigations_router
from app.api.v1.notifications import create_notifications_router
from app.api.v1.catalog import create_catalog_router
from app.api.v1.noise import create_noise_router
from app.api.v1.analytics import create_analytics_router
from app.api.v1.platform_health import create_platform_health_router
from app.application.sources import (
    ApplyCollection,
    CollectSource,
    EndpointReader,
    SourceJobPlanner,
    TestSource,
)
from app.application.observability import BackfillJobPlanner, BackfillSourceMetrics
from app.application.notifications import (
    ActivationTokenService,
    DeliverNotifications,
    NotificationJobPlanner,
)
from app.application.events import EventQueryService
from app.application.commands import IdempotentCommands
from app.application.jobs import (
    EnqueueOnlyScheduler,
    EnqueueOnlySchedulerState,
    JobHandlerRegistry,
    JobPolicyRegistry,
    JobQueue,
    JobRunner,
    JobRunnerState,
    create_job_runtime_readiness_probes,
)
from app.application.retention import RetentionCoordinator, RetentionPass
from app.bootstrap.app_factory import create_platform_app
from app.bootstrap.wiring import default_platform_wiring
from app.domains.operations.jobs import (
    ConcurrencyBudgets,
    JobPool,
    JobSpec,
    JobView,
    LeaseExpiryAction,
)
from app.domains.investigations.planner import PlannerReplyV1, PlannerToolCallV1
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database
from app.platform.persistence.probes import create_persistence_readiness_probes
from app.platform.metrics import MetricRegistry
from app.platform.runtime import PlatformRuntimeState
from app.platform.cursor import SignedCursorCodec
from app.crypto import SecretBox
from app.providers.model.base import ModelCallError, ModelClient
from app.providers.model.openai_compatible import OpenAICompatibleConfig
from app.providers.model.registry import get_model_client


@dataclass(frozen=True, slots=True)
class JobPlatformResources:
    app: FastAPI
    engine: Engine
    queue: JobQueue
    events: EventQueryService
    scheduler: EnqueueOnlyScheduler
    runner: JobRunner
    sources: SqlAlchemySourceStore
    observability: SqlAlchemyObservabilityStore
    notifications: SqlAlchemyNotificationStore
    incidents: SqlAlchemyIncidentStore
    investigations: SqlAlchemyInvestigationStore
    unified_investigations: SqlAlchemyUnifiedInvestigationStore
    catalog: SqlAlchemyServiceCatalogStore
    noise: SqlAlchemyNoiseStore
    analytics: SqlAlchemyAnalyticsStore


def create_job_platform_app(
    *,
    database_path: Path,
    master_key_path: Path,
    cursor_secret: bytes,
    source_reader: EndpointReader | None = None,
    thanos_factory: Any | None = None,
    grafana_factory: Any | None = None,
    model_probe: Any | None = None,
    model_client_factory: Any | None = None,
    investigator_runtime: Any | None = None,
    model_fake_mode: bool = False,
    notification_fake_mode: bool = False,
    notification_provider_factory: Any | None = None,
    workbench_url: str = "",
    frontend_dist: Path | None = None,
    trusted_hosts: tuple[str, ...] = (),
) -> JobPlatformResources:
    """Build the isolated platform without consulting the current product database."""
    database = Path(database_path).expanduser().resolve()
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=database))
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    job_repository = JobRepository()
    store = SqlAlchemyJobStore(sessions, repository=job_repository)
    commands = IdempotentCommands(SqlAlchemyCommandReceiptStore(sessions))
    codec = SignedCursorCodec(cursor_secret)
    def candidate_secret_box() -> SecretBox:
        return SecretBox(Path(master_key_path).read_text(encoding="utf-8").strip())

    def encrypt_source_secret(value: str) -> str:
        return json.dumps(
            candidate_secret_box().encrypt(value),
            sort_keys=True,
            separators=(",", ":"),
        )

    def decrypt_source_secret(value: str) -> str:
        raw: Any = json.loads(value)
        if not isinstance(raw, dict):
            raise RuntimeError("SOURCE_SECRET_INVALID")
        return candidate_secret_box().decrypt(cast(dict[str, Any], raw))

    noise_store = SqlAlchemyNoiseStore(sessions)
    notification_store = SqlAlchemyNotificationStore(
        sessions,
        decrypt_secret=decrypt_source_secret,
        encrypt_secret=encrypt_source_secret,
        response_state_reader=occurrence_response_state_in_session,
        enrichment_reader=read_notification_enrichment_in_session,
        noise_decider=noise_store.resolve_notification_noise_in_session,
        storm_summary_claim=noise_store.claim_storm_summary_in_session,
        workbench_url=workbench_url,
        execution_mode="FAKE" if notification_fake_mode else "EXTERNAL",
    )
    catalog_store = SqlAlchemyServiceCatalogStore(
        sessions, jobs=job_repository
    )
    investigation_store = SqlAlchemyInvestigationStore(
        sessions,
        jobs=job_repository,
        encrypt_invalid_raw=encrypt_source_secret,
    )
    unified_investigation_store = SqlAlchemyUnifiedInvestigationStore(
        sessions, jobs=job_repository
    )

    def cancel_occurrence_investigations(
        session: Any, occurrence_id: int, now: datetime
    ) -> None:
        investigation_store.request_cancel_in_session(session, occurrence_id, now)
        unified_investigation_store.request_cancel_for_occurrence_in_session(
            session, occurrence_id, now
        )

    incident_store = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=codec,
        notifications=notification_store,
        encrypt_secret=encrypt_source_secret,
        decrypt_secret=decrypt_source_secret,
        service_assignment_resolver=catalog_store.resolve_incident_assignment_in_session,
        service_descriptor=catalog_store.describe_assignment_in_session,
        active_service_lookup=catalog_store.active_service_in_session,
        noise_descriptor=noise_store.occurrence_noise_in_session,
        investigation_cancel=cancel_occurrence_investigations,
    )
    source_store = SqlAlchemySourceStore(
        sessions,
        decrypt_secret=decrypt_source_secret,
        encrypt_secret=encrypt_source_secret,
        incident_reconciler=incident_store,
        noise_observer=noise_store,
    )
    observability_store = SqlAlchemyObservabilityStore(
        sessions,
        decrypt_secret=decrypt_source_secret,
        encrypt_secret=encrypt_source_secret,
    )
    observability_store.seed_builtin_metric_templates(now=datetime.now(timezone.utc))
    analytics_store = SqlAlchemyAnalyticsStore(sessions)
    retention = RetentionCoordinator(
        SqlAlchemyRetentionPersistence(
            sessions,
            analytics=analytics_store,
            investigations=investigation_store,
        )
    )
    queue = JobQueue(store)
    events = EventQueryService(store, codec)
    scheduler_state = EnqueueOnlySchedulerState()
    runner_state = JobRunnerState()
    scheduler = EnqueueOnlyScheduler(queue, scheduler_state)
    source_jobs = SourceJobPlanner(source_store)
    scheduler.schedule_spec_supplier(
        identifier="sources.due",
        seconds=5,
        supplier=lambda: source_jobs.plan(now=datetime.now(timezone.utc)),
    )
    scheduler.schedule_spec_supplier(
        identifier="analytics.rollup",
        seconds=3600,
        supplier=lambda: (
            JobSpec(
                kind="analytics.rollup",
                pool=JobPool.SOURCE,
                subject_type="analytics",
                subject_id="overview",
                payload={},
                payload_revision=1,
                idempotency_key=(
                    "analytics-rollup-v1:"
                    f"{int(datetime.now(timezone.utc).timestamp()) // 3600}"
                ),
            ),
        ),
    )
    backfill_jobs = BackfillJobPlanner(observability_store)
    scheduler.schedule_spec_supplier(
        identifier="metrics.backfill",
        seconds=60,
        supplier=lambda: backfill_jobs.plan(now=datetime.now(timezone.utc)),
    )
    notification_jobs = NotificationJobPlanner(notification_store)
    scheduler.schedule_spec_supplier(
        identifier="notifications.outbox",
        seconds=5,
        supplier=lambda: notification_jobs.plan(now=datetime.now(timezone.utc)),
    )
    def initial_retention_spec() -> tuple[JobSpec, ...]:
        retention_pass = RetentionPass(
            as_of=datetime.now(timezone.utc),
            pass_number=0,
            rollups_complete=False,
        )
        return (
            JobSpec(
                kind="operations.retention",
                pool=JobPool.SOURCE,
                subject_type="operations",
                subject_id="retention",
                payload=retention_pass.to_payload(),
                payload_revision=3,
                idempotency_key=retention_pass.idempotency_key,
            ),
        )

    scheduler.schedule_spec_supplier(
        identifier="operations.retention",
        seconds=3600,
        supplier=initial_retention_spec,
    )
    metrics = MetricRegistry()
    runtime = PlatformRuntimeState()
    configured_source_reader = source_reader or BoundedAlertmanagerReader()

    async def collect_source_job(_job: JobView, payload: dict[str, object]) -> None:
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("source collect payload revision is invalid")
        source_id = _job.subject_id
        snapshot = source_store.load_snapshot(
            source_id,
            expected_version=expected_version,
        )
        observation = await CollectSource(
            source_store,
            configured_source_reader,
        ).execute(
            source_id,
            expected_version=expected_version,
        )
        applied = ApplyCollection(source_store).execute(
            snapshot,
            observation,
            observed_at=datetime.now(timezone.utc),
        )
        if not applied.committed:
            raise RuntimeError(applied.safe_error_code or "SOURCE_APPLY_REJECTED")

    async def backfill_metrics_job(_job: JobView, payload: dict[str, object]) -> None:
        expected_version = payload.get("expected_connection_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("metrics backfill payload revision is invalid")
        configured = observability_store.load_monitoring_secret(
            _job.subject_id, "THANOS", require_active=True
        )
        await BackfillSourceMetrics(observability_store).execute(
            _job.subject_id,
            configured_thanos_factory(configured.view.base_url, configured.secret),
            expected_connection_version=expected_version,
            now=datetime.now(timezone.utc),
        )

    configured_notification_providers = (
        notification_provider_factory or NotificationProviderRegistry()
    )

    async def deliver_notifications_job(
        _job: JobView, payload: dict[str, object]
    ) -> None:
        del _job, payload
        current = datetime.now(timezone.utc)
        notification_store.plan_due_reminders(now=current)
        await DeliverNotifications(
            notification_store,
            configured_notification_providers,
        ).execute(now=current)

    async def retention_job(_job: JobView, payload: dict[str, object]) -> None:
        if _job.payload_revision != 3:
            raise ValueError("RETENTION_PAYLOAD_REVISION_UNSUPPORTED")
        result = retention.run_pass(RetentionPass.from_payload(payload))
        if result.next_pass is not None:
            queue.enqueue(
                JobSpec(
                    kind="operations.retention",
                    pool=JobPool.SOURCE,
                    subject_type="operations",
                    subject_id="retention",
                    payload=result.next_pass.to_payload(),
                    payload_revision=3,
                    idempotency_key=result.next_pass.idempotency_key,
                )
            )

    async def analytics_rollup_job(
        _job: JobView, payload: dict[str, object]
    ) -> None:
        del _job, payload
        analytics_store.refresh(now=datetime.now(timezone.utc))

    async def enqueue_initial_analytics() -> None:
        current = datetime.now(timezone.utc)
        queue.enqueue(
            JobSpec(
                kind="analytics.rollup",
                pool=JobPool.SOURCE,
                subject_type="analytics",
                subject_id="overview",
                payload={},
                payload_revision=1,
                idempotency_key=(
                    f"analytics-rollup-v1:{int(current.timestamp()) // 3600}"
                ),
            )
        )

    async def service_mapping_reprojection_job(
        _job: JobView, payload: dict[str, object]
    ) -> None:
        del _job, payload
        catalog_store.reproject_open_occurrences()

    class _PlannerModelAdapter:
        def __init__(self, client: ModelClient) -> None:
            self._client = client

        async def complete(
            self,
            *,
            messages: Sequence[Mapping[str, Any]],
            tools: Sequence[Mapping[str, Any]] = (),
            response_format: Mapping[str, Any] | None = None,
        ) -> PlannerReplyV1:
            try:
                reply = await self._client.complete(
                    messages=messages,
                    tools=tools,
                    response_format=response_format,
                )
            except ModelCallError as exc:
                raise PlannerModelCallError(exc.kind.value) from None
            return PlannerReplyV1(
                text=reply.text,
                tool_calls=tuple(
                    PlannerToolCallV1(item.call_id, item.name, item.arguments)
                    for item in reply.tool_calls
                ),
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
            )

    def default_model_client_factory(
        kind: str, target: PlannerModelTarget
    ) -> _PlannerModelAdapter:
        return _PlannerModelAdapter(get_model_client(
            kind,
            OpenAICompatibleConfig(
                base_url=target.base_url,
                model=target.model,
                api_key=target.api_key,
            ),
        ))

    configured_model_client_factory = (
        model_client_factory or default_model_client_factory
    )
    evidence_expansion = RunEvidenceExpansion(
        store=investigation_store,
        observability=observability_store,
        thanos_factory=lambda base_url, secret: configured_thanos_factory(
            base_url, secret
        ),
        model_client_factory=configured_model_client_factory,
    )
    configured_investigator_runtime = investigator_runtime or (
        offline_investigator_runtime(
            journal_path=database.with_suffix(".investigator-harness.db")
        )
        if model_fake_mode
        else PydanticInvestigatorRuntime(
            journal_path=database.with_suffix(".investigator-harness.db")
        )
    )
    unified_investigator = RunUnifiedInvestigation(
        store=unified_investigation_store,
        observability=observability_store,
        thanos_factory=lambda base_url, secret: configured_thanos_factory(
            base_url, secret
        ),
        runtime=configured_investigator_runtime,
    )

    async def investigation_expansion_job(
        job: JobView, payload: dict[str, object]
    ) -> None:
        if payload.get("investigation_id") != job.subject_id:
            raise ValueError("investigation expansion payload is invalid")
        await evidence_expansion.execute(job.subject_id)

    async def unified_investigation_job(
        job: JobView, payload: dict[str, object]
    ) -> None:
        if payload.get("investigation_id") != job.subject_id:
            raise ValueError("unified investigation payload is invalid")
        await unified_investigator.execute(job.subject_id)

    runner = JobRunner(
        queue=queue,
        handlers=JobHandlerRegistry(
            {
                "source.collect": collect_source_job,
                "metrics.backfill": backfill_metrics_job,
                "notification.dispatch": deliver_notifications_job,
                "operations.retention": retention_job,
                "analytics.rollup": analytics_rollup_job,
                "service-mapping.reproject": service_mapping_reprojection_job,
                "investigation.expand-evidence": investigation_expansion_job,
                "investigation.run-v2": unified_investigation_job,
            }
        ),
        policies=JobPolicyRegistry(
            lease_expiry={
                "source.collect": LeaseExpiryAction.REQUEUE,
                "metrics.backfill": LeaseExpiryAction.REQUEUE,
                "notification.dispatch": LeaseExpiryAction.REQUEUE,
                "operations.retention": LeaseExpiryAction.REQUEUE,
                "analytics.rollup": LeaseExpiryAction.REQUEUE,
                "service-mapping.reproject": LeaseExpiryAction.REQUEUE,
                "investigation.expand-evidence": LeaseExpiryAction.REQUEUE,
                "investigation.run-v2": LeaseExpiryAction.REQUEUE,
            }
        ),
        state=runner_state,
        budgets=ConcurrencyBudgets(),
        owner_prefix="candidate",
    )
    probes = create_persistence_readiness_probes(
        engine=engine,
        database_path=database,
        master_key_path=master_key_path,
    ) + create_job_runtime_readiness_probes(
        queue=queue,
        scheduler=scheduler_state,
        runner=runner_state,
        metrics=metrics,
    )

    async def dispose() -> None:
        engine.dispose()

    router = create_api_v1_router(job_queries=queue, event_queries=events)
    configured_thanos_factory = thanos_factory or (
        lambda base_url, secret: BoundedThanosReader(base_url, token=secret)
    )
    configured_grafana_factory = grafana_factory or (
        lambda base_url, secret: BoundedGrafanaReader(base_url, token=secret)
    )

    async def historical_labels(
        lookback_hours: int,
    ) -> tuple[
        tuple[dict[str, str], ...],
        Literal["ok", "unconfigured", "error"],
        str | None,
    ]:
        connections = observability_store.list_active_monitoring_connections("THANOS")
        if not connections:
            return (), "unconfigured", None
        end = datetime.now(timezone.utc)
        history: list[dict[str, str]] = []
        errors: list[str] = []
        per_source_limit = max(1, 5000 // len(connections))
        for connection in connections:
            try:
                configured = observability_store.load_monitoring_secret(
                    connection.source_id,
                    "THANOS",
                    require_active=True,
                )
                reader = configured_thanos_factory(
                    configured.view.base_url,
                    configured.secret,
                )
                values = await reader.series_alerts(
                    end - timedelta(hours=lookback_hours),
                    end,
                    limit=per_source_limit,
                )
                history.extend(values)
            except Exception as exc:  # noqa: BLE001 - current catalog remains usable
                errors.append(
                    str(getattr(exc, "code", "THANOS_LABEL_DISCOVERY_FAILED"))[:96]
                )
        return (
            tuple(history),
            "error" if errors else "ok",
            None if not errors else sorted(set(errors))[0],
        )

    sources_router = create_sources_router(
        port=source_store,
        jobs=queue,
        tester=TestSource(source_store, configured_source_reader),
        commands=commands,
        historical_label_reader=historical_labels,
    )
    observability_router = create_observability_router(
        port=observability_store,
        thanos_factory=configured_thanos_factory,
        grafana_factory=configured_grafana_factory,
        model_probe=model_probe or PydanticInvestigatorProbe(),
        model_fake_mode=model_fake_mode,
        commands=commands,
    )
    notifications_router = create_notifications_router(
        store=notification_store,
        providers=configured_notification_providers,
        activation_tokens=ActivationTokenService(key=cursor_secret),
        commands=commands,
    )
    incidents_router = create_incidents_router(
        store=incident_store,
        response_commands=IncidentResponseCommands(incident_store),
        collaboration_commands=IncidentCollaborationCommands(incident_store),
        service_assignment_commands=IncidentServiceAssignmentCommands(incident_store),
    )
    investigations_router = create_investigations_router(
        store=investigation_store,
        starter=StartInvestigation(
            store=investigation_store,
            incidents=incident_store,
            observability=observability_store,
            thanos_factory=configured_thanos_factory,
            model_execution_mode="FAKE" if model_fake_mode else "EXTERNAL",
        ),
    )
    unified_investigations_router = create_unified_investigations_router(
        store=unified_investigation_store,
        starter=StartUnifiedInvestigation(
            store=unified_investigation_store,
            incidents=incident_store,
            observability=observability_store,
            thanos_factory=configured_thanos_factory,
            model_execution_mode="FAKE" if model_fake_mode else "EXTERNAL",
        ),
    )
    catalog_router = create_catalog_router(port=catalog_store, commands=commands)
    noise_router = create_noise_router(port=noise_store, commands=commands)
    analytics_router = create_analytics_router(store=analytics_store)
    platform_health_router = create_platform_health_router(
        reader=SqlAlchemyPlatformHealthReader(
            sessions,
            runtime=runtime,
            probes=probes,
            scheduler=scheduler_state,
            runner=runner_state,
        )
    )
    wiring = default_platform_wiring(
        runtime=runtime,
        metrics=metrics,
        readiness_probes=probes,
        routers=(
            router,
            sources_router,
            observability_router,
            notifications_router,
            incidents_router,
            investigations_router,
            unified_investigations_router,
            catalog_router,
            noise_router,
            analytics_router,
            platform_health_router,
        ),
        startup_hooks=(enqueue_initial_analytics, scheduler.start, runner.start),
        shutdown_hooks=(dispose, runner.stop, scheduler.stop),
    )
    return JobPlatformResources(
        app=create_platform_app(
            wiring=wiring,
            frontend_dist=frontend_dist,
            trusted_hosts=trusted_hosts,
        ),
        engine=engine,
        queue=queue,
        events=events,
        scheduler=scheduler,
        runner=runner,
        sources=source_store,
        observability=observability_store,
        notifications=notification_store,
        incidents=incident_store,
        investigations=investigation_store,
        unified_investigations=unified_investigation_store,
        catalog=catalog_store,
        noise=noise_store,
        analytics=analytics_store,
    )
