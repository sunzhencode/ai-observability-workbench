"""FastAPI application entrypoint with the polling scheduler."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.api import (
    aggregation_rules,
    alert_investigation,
    alerts,
    event_sources,
    grafana_import,
    health,
    incident_occurrences,
    incidents,
    metrics,
    model_channels,
    notification_channels,
    notification_deliveries,
    notification_policies,
    panel_mappings,
    policies,
    settings as settings_api,
)
from app.db import get_engine, init_db
from app.config_schemas import safe_validation_exception_handler
from app.runtime_config import runtime_config_provider
from app.services.ingest import ingest_alerts
from app.services.notification_planner import (
    plan_due_reminders,
    reconcile_incident_changes,
    snapshot_incidents,
)
from app.services.source_polling import (
    PollCoordinator,
    has_enabled_event_sources,
)
from app.services.thanos_history import enabled_thanos_connections
from app.services.retention import cleanup_expired_data
from app.services.backfill import backfill_window, thanos_matrix_to_alerts
from app.sources.thanos import ThanosClient
from app.services.delivery_worker import DeliveryWorker
from app.state import backfill_status, notification_status, poll_status

logger = logging.getLogger("alert_workbench")

scheduler = AsyncIOScheduler()
ingest_lock = asyncio.Lock()
# Delivery worker tick. Fast enough to feel immediate, slow enough to idle.
NOTIFICATION_WORKER_INTERVAL_SECONDS = 5
# Per-source intervals can be configured down to 5 seconds, so the scheduler
# must offer every source a due-check at least that often.
POLL_SCHEDULER_TICK_SECONDS = 5

poll_lock = asyncio.Lock()
delivery_lock = asyncio.Lock()
source_poll_coordinator = PollCoordinator(get_engine)


async def backfill_once() -> None:
    """Reconstruct bounded alert history without blocking live polling.

    History is read per source from the address configured on that source
    (F22). A source without one contributes nothing: an unattributable read
    cannot become that source's history.
    """
    runtime = runtime_config_provider.snapshot()
    now = datetime.now(timezone.utc)
    window = backfill_window(
        now,
        requested_hours=runtime.backfill_hours,
        hard_limit_hours=runtime.backfill_max_hours,
    )
    backfill_status.last_backfill_at = now
    backfill_status.effective_hours = window.effective_hours
    backfill_status.truncated_reason = window.truncated_reason
    backfill_status.alerts_reconstructed = 0

    with Session(get_engine()) as config_session:
        connections = enabled_thanos_connections(config_session)

    if not connections:
        backfill_status.skipped = True
        backfill_status.last_backfill_ok = None
        backfill_status.last_backfill_error = None
        logger.info("Thanos backfill skipped: no source has a usable history address")
        return

    backfill_status.skipped = False
    scoped_results: list[tuple[str | None, list[dict]]] = []
    errors: list[str] = []
    for connection in connections:
        if not connection.usable:
            errors.append(
                f"{connection.source_id}:{connection.error_code or 'CONNECTION_UNUSABLE'}"
            )
            continue
        try:
            client = ThanosClient(
                base_url=connection.base_url,
                token=connection.token,
                timeout=connection.timeout_seconds,
            )
            payload = await client.query_alerts(
                start=window.start,
                end=window.end,
                step_seconds=runtime.backfill_step_seconds,
            )
            scoped_results.append(
                (connection.source_id, thanos_matrix_to_alerts(payload))
            )
        except Exception as exc:  # noqa: BLE001 - isolate each source
            errors.append(f"{connection.source_id}:QUERY_FAILED")
            logger.warning(
                "Thanos backfill failed for source=%s error_type=%s",
                connection.source_id,
                type(exc).__name__,
            )

    try:
        reconstructed = sum(len(items) for _, items in scoped_results)
        if scoped_results:
            async with ingest_lock:
                with Session(get_engine()) as session:
                    for source_id, raw_alerts in scoped_results:
                        ingest_alerts(
                            session,
                            raw_alerts,
                            poll_time=now,
                            source_id=source_id,
                            origin="backfill",
                            evidence_completeness="reconstructed",
                            reconcile_lifecycle=False,
                            runtime=runtime,
                        )
                    session.commit()
        backfill_status.last_backfill_ok = not errors
        backfill_status.last_backfill_error = ",".join(errors) if errors else None
        backfill_status.alerts_reconstructed = reconstructed
        logger.info(
            "Thanos backfill completed: reconstructed=%s source_errors=%s",
            reconstructed,
            len(errors),
        )
    except Exception as exc:  # noqa: BLE001 - local UoW failure must not stop live poll
        backfill_status.last_backfill_ok = False
        backfill_status.last_backfill_error = "LOCAL_PERSISTENCE_FAILED"
        logger.warning(
            "Thanos backfill persistence failed: error_type=%s",
            type(exc).__name__,
        )


async def retention_once() -> None:
    runtime = runtime_config_provider.snapshot()
    now = datetime.now(timezone.utc)
    async with ingest_lock:
        with Session(get_engine()) as session:
            result = cleanup_expired_data(
                session,
                now=now,
                retention_days=runtime.retention_days,
                occurrence_history_retention_days=(
                    runtime.occurrence_history_retention_days
                ),
            )
    logger.info(
        "Retention cleanup: alerts=%s endpoint_observations=%s audits=%s policies=%s alert_type_rules=%s incidents=%s deliveries=%s attempts=%s routes=%s targets=%s config_audits=%s occurrence_history=%s",
        result.alerts_deleted,
        result.endpoint_observations_deleted,
        result.audits_deleted,
        result.policies_deleted,
        result.alert_type_rules_deleted,
        result.incidents_deleted,
        result.notification_deliveries_deleted,
        result.notification_attempts_deleted,
        result.notification_routes_deleted,
        result.notification_route_targets_deleted,
        result.config_audits_deleted,
        result.occurrence_history_deleted,
    )


async def notification_delivery_once() -> None:
    """Plan due reminders, then deliver committed Outbox rows outside poll."""
    async with delivery_lock:
        now = datetime.now(timezone.utc)
        try:
            async with ingest_lock:
                with Session(get_engine()) as session:
                    plan_due_reminders(session, now=now)
                    session.commit()
            worker = DeliveryWorker(get_engine())
            await worker.run_once(now=now)
            notification_status.last_error_code = None
        except Exception:  # noqa: BLE001 - notification failure must not poison poll
            notification_status.last_error_code = "WORKER_ERROR"
            logger.exception("Notification delivery worker failed")
        finally:
            notification_status.last_run_at = datetime.now(timezone.utc)


async def poll_once() -> None:
    """Serialize complete AM jobs so source revisions can never overlap."""
    async with poll_lock:
        await _poll_once_locked()


async def _poll_once_locked() -> None:
    """Poll every enabled Event Source. The registry is the only source of truth.

    An empty registry polls nothing rather than falling back to `.env`; that
    dual path is what produced the R1 defect. See ADR 0004.
    """
    poll_time = datetime.now(timezone.utc)
    outcomes = await source_poll_coordinator.poll_enabled_sources(now=poll_time)

    if not outcomes:
        with Session(get_engine()) as session:
            configured = has_enabled_event_sources(session)
        if configured:
            logger.debug("Poll tick skipped: no enabled source is due")
            return
        poll_status.last_poll_at = poll_time
        poll_status.source_id = None
        poll_status.watchdog_current_clusters = set()
        poll_status.last_poll_ok = None
        poll_status.last_poll_error = "no enabled event source"
        logger.info("Poll skipped: registry has no enabled event source")
        return

    poll_status.last_poll_at = poll_time
    poll_status.source_id = None
    poll_status.watchdog_current_clusters = set()
    poll_status.last_poll_ok = all(
        item.committed and item.completeness == "COMPLETE" for item in outcomes
    )
    errors = [
        item.error_code or item.completeness
        for item in outcomes
        if not item.committed or item.completeness != "COMPLETE"
    ]
    poll_status.last_poll_error = ",".join(errors) if errors else None
    logger.info(
        "Poll completed: sources=%s complete=%s partial=%s failed=%s",
        len(outcomes),
        sum(item.completeness == "COMPLETE" and item.committed for item in outcomes),
        sum(item.completeness == "PARTIAL" and item.committed for item in outcomes),
        sum(item.completeness == "FAILED" or not item.committed for item in outcomes),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    now = datetime.now(timezone.utc)
    scheduler.add_job(
        poll_once,
        "interval",
        seconds=POLL_SCHEDULER_TICK_SECONDS,
        id="poll_alertmanager",
        next_run_time=now,
    )
    scheduler.add_job(backfill_once, "date", run_date=now, id="backfill_thanos")
    scheduler.add_job(
        retention_once,
        "interval",
        hours=24,
        id="retention_cleanup",
        next_run_time=now + timedelta(hours=24),
    )
    scheduler.add_job(
        notification_delivery_once,
        "interval",
        seconds=NOTIFICATION_WORKER_INTERVAL_SECONDS,
        id="notification_delivery",
        next_run_time=now + timedelta(
            seconds=NOTIFICATION_WORKER_INTERVAL_SECONDS
        ),
    )
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Alert Workbench", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(RequestValidationError, safe_validation_exception_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(incidents.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(event_sources.router, prefix="/api")
app.include_router(panel_mappings.router, prefix="/api")
app.include_router(policies.router, prefix="/api")
app.include_router(aggregation_rules.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")
app.include_router(notification_channels.router, prefix="/api")
app.include_router(notification_policies.router, prefix="/api")
app.include_router(notification_deliveries.router, prefix="/api")
app.include_router(incident_occurrences.router, prefix="/api")
app.include_router(metrics.router, prefix="/api")
app.include_router(alert_investigation.router, prefix="/api")
app.include_router(model_channels.router, prefix="/api")
app.include_router(grafana_import.router, prefix="/api")
