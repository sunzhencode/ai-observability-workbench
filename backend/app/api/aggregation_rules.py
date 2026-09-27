"""Standalone aggregation-rule management and global label discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.db import get_session
from app.models import AggregationRule, Alert
from app.schemas import (
    AggregationLabelCatalogOut,
    AggregationLabelOut,
    AggregationPreviewGroupOut,
    AggregationPreviewSourceOut,
    AggregationRuleOut,
    AggregationRulePreviewOut,
    AggregationRulePreviewPut,
    AggregationRulePut,
)
from app.services.aggregation_rules import (
    create_aggregation_rule,
    list_aggregation_rules,
    preview_aggregation_rule,
    update_aggregation_rule,
)
from app.services.discovery import summarize_global_labels
from app.services.source_scope import aggregation_rule_scope, visible_source_ids
from app.services.thanos_history import enabled_thanos_connections
from app.sources.thanos import ThanosClient, safe_error_code

router = APIRouter()
ALLOWED_LOOKBACK_HOURS = {24, 168, 720}
# A bound on one read-only catalog query, not something to tune per install.
LABEL_DISCOVERY_MAX_SERIES = 5000


def get_thanos_client(session: Session = Depends(get_session)) -> ThanosClient:
    """Build a read-only Thanos client from the registry.

    Label discovery is a cross-source catalog, so it uses the first enabled
    source that has a usable history address. With none configured the client
    reports ``configured == False`` and the catalog degrades to current alerts
    only.
    """
    for connection in enabled_thanos_connections(session):
        if connection.usable:
            return ThanosClient(
                base_url=connection.base_url,
                token=connection.token,
                timeout=connection.timeout_seconds,
            )
    return ThanosClient()


def _rule_out(session: Session, rule: AggregationRule) -> AggregationRuleOut:
    if rule.id is None:
        raise RuntimeError("persisted aggregation rule has no id")
    return AggregationRuleOut(
        id=rule.id,
        name=rule.name,
        priority=rule.priority,
        enabled=rule.enabled,
        matchers=list(rule.matchers),
        group_by_labels=list(rule.group_by_labels),
        source_scope=aggregation_rule_scope(session, rule.id, rule.scope_mode),
        version=rule.version,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _matcher_dicts(payload: AggregationRulePut) -> list[dict[str, str]]:
    return [matcher.model_dump() for matcher in payload.matchers]


@router.get("/aggregation-rules", response_model=list[AggregationRuleOut])
def get_aggregation_rules(
    session: Session = Depends(get_session),
) -> list[AggregationRuleOut]:
    return [_rule_out(session, rule) for rule in list_aggregation_rules(session)]


@router.post(
    "/aggregation-rules/preview",
    response_model=AggregationRulePreviewOut,
)
def preview_rule(
    payload: AggregationRulePreviewPut,
    session: Session = Depends(get_session),
) -> AggregationRulePreviewOut:
    try:
        preview = preview_aggregation_rule(
            session,
            rule_id=payload.rule_id,
            name=payload.name,
            priority=payload.priority,
            enabled=payload.enabled,
            matchers=_matcher_dicts(payload),
            group_by_labels=payload.group_by_labels,
            source_scope=payload.source_scope.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AggregationRulePreviewOut(
        matcher_alert_count=preview.matcher_alert_count,
        selected_alert_count=preview.selected_alert_count,
        proposed_group_count=preview.proposed_group_count,
        groups=[
            AggregationPreviewGroupOut.model_validate(group.__dict__)
            for group in preview.groups
        ],
        by_source=[
            AggregationPreviewSourceOut.model_validate(item)
            for item in preview.by_source
        ],
    )


@router.post(
    "/aggregation-rules",
    response_model=AggregationRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_rule(
    payload: AggregationRulePut,
    session: Session = Depends(get_session),
) -> AggregationRuleOut:
    try:
        rule = create_aggregation_rule(
            session,
            name=payload.name,
            priority=payload.priority,
            enabled=payload.enabled,
            matchers=_matcher_dicts(payload),
            group_by_labels=payload.group_by_labels,
            source_scope=payload.source_scope.model_dump(),
        )
        session.commit()
        session.refresh(rule)
    except FileExistsError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _rule_out(session, rule)


@router.put("/aggregation-rules/{rule_id}", response_model=AggregationRuleOut)
def update_rule(
    rule_id: int,
    payload: AggregationRulePut,
    session: Session = Depends(get_session),
) -> AggregationRuleOut:
    try:
        rule = update_aggregation_rule(
            session,
            rule_id,
            name=payload.name,
            priority=payload.priority,
            enabled=payload.enabled,
            matchers=_matcher_dicts(payload),
            group_by_labels=payload.group_by_labels,
            source_scope=payload.source_scope.model_dump(),
        )
        session.commit()
        session.refresh(rule)
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _rule_out(session, rule)


@router.get("/aggregation-labels", response_model=AggregationLabelCatalogOut)
async def get_aggregation_labels(
    lookback_hours: int = Query(default=168),
    session: Session = Depends(get_session),
    thanos: ThanosClient = Depends(get_thanos_client),
) -> AggregationLabelCatalogOut:
    if lookback_hours not in ALLOWED_LOOKBACK_HOURS:
        raise HTTPException(status_code=422, detail="lookback_hours must be 24, 168, or 720")
    source_ids = visible_source_ids(session, include_archived=False)
    current = session.exec(
        select(Alert)
        .where(Alert.source_id.in_(source_ids))
        .order_by(Alert.fingerprint)
    ).all()
    history: list[dict[str, str]] = []
    history_status = "unconfigured"
    history_error: str | None = None
    if thanos.configured:
        end = datetime.now(timezone.utc)
        try:
            history = await thanos.series_alerts(
                start=end - timedelta(hours=lookback_hours),
                end=end,
                limit=LABEL_DISCOVERY_MAX_SERIES,
            )
            history_status = "ok"
        except Exception as exc:  # noqa: BLE001 - current catalog remains usable
            history_status = "error"
            # A safe code, never `str(exc)`: an httpx error carries the full URL,
            # so the internal Thanos host, port and query string would land in
            # this response body. §7 already forbids that for backfill health.
            history_error = safe_error_code(exc)
    labels = summarize_global_labels(current, history)
    return AggregationLabelCatalogOut(
        lookback_hours=lookback_hours,
        history_status=history_status,
        history_error=history_error,
        history_series_count=len(history),
        labels=[AggregationLabelOut.model_validate(item.__dict__) for item in labels],
    )
