"""F20 EventSource and Endpoint configuration APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config_schemas import (
    EventSourceAuditOut,
    EventSourceCandidateIn,
    EventSourceCreateIn,
    EventSourceOut,
    EventSourceTestIn,
    EventSourceTestOut,
    EventSourceUpdateIn,
    RevisionActionIn,
    WatchdogClusterCreateIn,
    WatchdogClusterOut,
    WatchdogClusterUpdateIn,
)
from app.crypto import SecretError, SecretUnavailableError
from app.db import get_session
from app.registry_models import EventSource
from app.services.event_sources import (
    EndpointTester,
    EventSourceTestFailed,
    HTTPAlertmanagerEndpointTester,
    archive_event_source,
    create_event_source,
    disable_event_source,
    enable_event_source,
    event_source_audit_public,
    event_source_public_dict,
    has_managed_event_source,
    list_event_sources,
    test_event_source,
    save_event_source_changes,
)
from app.services.watchdog import (
    add_expected_cluster,
    list_watchdog_clusters,
    update_cluster_inventory_state,
)

router = APIRouter()


def get_endpoint_tester() -> EndpointTester:
    return HTTPAlertmanagerEndpointTester()


def _candidate(payload: EventSourceCandidateIn) -> dict[str, Any]:
    return {
        "name": payload.name,
        "endpoints": [
            {
                "position": item.position,
                "url": item.url,
                "enabled": item.enabled,
                "auth_type": item.auth_type,
                "username": item.username,
                "secret_action": item.secret.action,
                "secret_value": item.secret.value,
            }
            for item in payload.endpoints
        ],
        "poll_interval_seconds": payload.poll_interval_seconds,
        "resolution_grace_seconds": payload.resolution_grace_seconds,
        "max_parallel_endpoints": payload.max_parallel_endpoints,
        "watchdog_enabled": payload.watchdog_enabled,
        "watchdog_alertname": payload.watchdog_alertname,
        "watchdog_identity_label": payload.watchdog_identity_label,
        "watchdog_missing_after_seconds": payload.watchdog_missing_after_seconds,
        "thanos": (
            {
                "url": payload.thanos.url,
                "auth_type": payload.thanos.auth_type,
                "username": payload.thanos.username,
                "secret_action": payload.thanos.secret.action,
                "secret_value": payload.thanos.secret.value,
                "timeout_seconds": payload.thanos.timeout_seconds,
            }
            if payload.thanos is not None
            else None
        ),
    }


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail="EVENT_SOURCE_NOT_FOUND")
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail="REVISION_CONFLICT")
    if isinstance(exc, EventSourceTestFailed):
        return HTTPException(status_code=422, detail=exc.result.code)
    if isinstance(exc, SecretUnavailableError):
        # Actionable: the local master key file could not be created or read,
        # and a bare SECRET_UNAVAILABLE never said so.
        return HTTPException(status_code=503, detail="MASTER_KEY_NOT_CONFIGURED")
    if isinstance(exc, SecretError):
        return HTTPException(status_code=503, detail="SECRET_UNAVAILABLE")
    if isinstance(exc, IntegrityError):
        return HTTPException(status_code=409, detail="CONFIGURATION_CONFLICT")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="EVENT_SOURCE_OPERATION_FAILED")


def _out(session: Session, source: EventSource) -> EventSourceOut:
    return EventSourceOut.model_validate(event_source_public_dict(session, source))


def _watchdog_cluster_out(
    session: Session, source_id: str, cluster_id: int | None = None
) -> WatchdogClusterOut:
    rows = list_watchdog_clusters(session, source_id)
    if cluster_id is None:
        if not rows:
            raise LookupError("monitored cluster not found")
        return WatchdogClusterOut.model_validate(rows[0])
    for row in rows:
        if row["id"] == cluster_id:
            return WatchdogClusterOut.model_validate(row)
    raise LookupError("monitored cluster not found")


@router.get("/event-sources", response_model=list[EventSourceOut])
def list_event_source_endpoint(
    include_archived: bool = False,
    session: Session = Depends(get_session),
) -> list[EventSourceOut]:
    return [
        EventSourceOut.model_validate(item)
        for item in list_event_sources(session, include_archived=include_archived)
    ]


@router.post(
    "/event-sources",
    response_model=EventSourceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_event_source_endpoint(
    payload: EventSourceCreateIn,
    session: Session = Depends(get_session),
) -> EventSourceOut:
    try:
        source = create_event_source(
            session,
            candidate=_candidate(payload),
            enable=payload.enable,
        )
        session.commit()
        session.refresh(source)
        return _out(session, source)
    except Exception as exc:
        session.rollback()
        raise _error(exc) from exc


@router.get("/event-sources/{source_id}", response_model=EventSourceOut)
def get_event_source_endpoint(
    source_id: str, session: Session = Depends(get_session)
) -> EventSourceOut:
    source = session.get(EventSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="EVENT_SOURCE_NOT_FOUND")
    return _out(session, source)


@router.patch("/event-sources/{source_id}", response_model=EventSourceOut)
def update_event_source_endpoint(
    source_id: str,
    payload: EventSourceUpdateIn,
    session: Session = Depends(get_session),
) -> EventSourceOut:
    try:
        source = save_event_source_changes(
            session,
            source_id,
            candidate=_candidate(payload),
            expected_version=payload.expected_version,
        )
        session.commit()
        session.refresh(source)
        return _out(session, source)
    except Exception as exc:
        session.rollback()
        raise _error(exc) from exc


@router.post("/event-sources/{source_id}/test", response_model=EventSourceTestOut)
async def test_event_source_endpoint(
    source_id: str,
    payload: EventSourceTestIn,
    session: Session = Depends(get_session),
    tester: EndpointTester = Depends(get_endpoint_tester),
) -> EventSourceTestOut:
    try:
        result = await test_event_source(
            session,
            source_id,
            candidate=_candidate(payload.candidate) if payload.candidate else None,
            positions=set(payload.positions) if payload.positions is not None else None,
            tester=tester,
        )
        session.commit()
        return EventSourceTestOut.model_validate(
            {
                "ok": result.ok,
                "code": result.code,
                "endpoints": [item.__dict__ for item in result.endpoints],
            }
        )
    except Exception as exc:
        session.rollback()
        raise _error(exc) from exc


def _action(
    session: Session,
    source_id: str,
    payload: RevisionActionIn,
    operation,
) -> EventSourceOut:
    try:
        source = operation(
            session, source_id, expected_version=payload.expected_version
        )
        session.commit()
        session.refresh(source)
        return _out(session, source)
    except Exception as exc:
        session.rollback()
        raise _error(exc) from exc


@router.post("/event-sources/{source_id}/enable", response_model=EventSourceOut)
def enable_event_source_endpoint(
    source_id: str,
    payload: RevisionActionIn,
    session: Session = Depends(get_session),
) -> EventSourceOut:
    return _action(session, source_id, payload, enable_event_source)


@router.post("/event-sources/{source_id}/disable", response_model=EventSourceOut)
def disable_event_source_endpoint(
    source_id: str,
    payload: RevisionActionIn,
    session: Session = Depends(get_session),
) -> EventSourceOut:
    return _action(session, source_id, payload, disable_event_source)


@router.post("/event-sources/{source_id}/archive", response_model=EventSourceOut)
def archive_event_source_endpoint(
    source_id: str,
    payload: RevisionActionIn,
    session: Session = Depends(get_session),
) -> EventSourceOut:
    return _action(session, source_id, payload, archive_event_source)


@router.get(
    "/event-sources/{source_id}/watchdog-clusters",
    response_model=list[WatchdogClusterOut],
)
def list_watchdog_cluster_endpoint(
    source_id: str, session: Session = Depends(get_session)
) -> list[WatchdogClusterOut]:
    try:
        return [
            WatchdogClusterOut.model_validate(item)
            for item in list_watchdog_clusters(session, source_id)
        ]
    except Exception as exc:
        raise _error(exc) from exc


@router.post(
    "/event-sources/{source_id}/watchdog-clusters",
    response_model=WatchdogClusterOut,
    status_code=status.HTTP_201_CREATED,
)
def add_expected_watchdog_cluster_endpoint(
    source_id: str,
    payload: WatchdogClusterCreateIn,
    session: Session = Depends(get_session),
) -> WatchdogClusterOut:
    try:
        cluster = add_expected_cluster(
            session, source_id, payload.identity_value
        )
        cluster_id = int(cluster.id)
        session.commit()
        return _watchdog_cluster_out(session, source_id, cluster_id)
    except Exception as exc:
        session.rollback()
        raise _error(exc) from exc


@router.patch(
    "/event-sources/{source_id}/watchdog-clusters/{cluster_id}",
    response_model=WatchdogClusterOut,
)
def update_watchdog_cluster_endpoint(
    source_id: str,
    cluster_id: int,
    payload: WatchdogClusterUpdateIn,
    session: Session = Depends(get_session),
) -> WatchdogClusterOut:
    try:
        cluster = update_cluster_inventory_state(
            session,
            source_id,
            cluster_id,
            action=payload.action,
            reason=payload.reason,
        )
        cluster_id = int(cluster.id)
        session.commit()
        return _watchdog_cluster_out(session, source_id, cluster_id)
    except Exception as exc:
        session.rollback()
        raise _error(exc) from exc


@router.get(
    "/event-sources/{source_id}/audit", response_model=list[EventSourceAuditOut]
)
def event_source_audit_endpoint(
    source_id: str, session: Session = Depends(get_session)
) -> list[EventSourceAuditOut]:
    try:
        return [
            EventSourceAuditOut.model_validate(item)
            for item in event_source_audit_public(session, source_id)
        ]
    except Exception as exc:
        raise _error(exc) from exc
