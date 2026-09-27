"""Legacy alert-root investigation kept during the platform transition.

The occurrence-root investigation is the target product path.  This endpoint
remains mounted only so the current Workbench keeps working until the default
runtime switches; it does not author or expose PromQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.db import get_session
from app.models import Alert, Incident, utcnow
from app.providers.model.base import ModelCallError
from app.registry_models import ModelCallLog
from app.schemas import MetricEvidenceOut
from app.services import handling_digest, investigation, model_channels
from app.services.evidence_context import source_visible
from app.services.model_channels import ActiveModel

router = APIRouter()


class RecommendationOut(BaseModel):
    kind: str
    text: str


class HypothesisOut(BaseModel):
    statement: str
    verdict: str
    supporting_fact_ids: list[str] = []
    contradicting_fact_ids: list[str] = []
    missing_evidence: list[str] = []
    recommendations: list[RecommendationOut] = []


class FactOut(BaseModel):
    fact_id: str
    statement: str


class InvestigationOut(BaseModel):
    hypotheses: list[HypothesisOut] = []
    facts: list[FactOut] = []
    failure: str | None = None
    possibly_billed: bool = False
    model_calls: int = 0
    model_name: str = ""
    prompt_version: str = ""


@router.post("/alerts/{alert_id}/investigate", response_model=InvestigationOut)
async def investigate_alert(
    alert_id: int, session: Session = Depends(get_session)
) -> InvestigationOut:
    """Read one alert's frozen evidence and produce attributed hypotheses."""

    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    incident = session.get(Incident, alert.incident_id) if alert.incident_id else None
    source_id = str(
        getattr(incident, "source_id", "") or getattr(alert, "source_id", "") or ""
    )
    if source_id and not source_visible(session, source_id):
        raise HTTPException(status_code=404, detail="Alert not found")

    active = model_channels.resolve_active_model(session)
    if active is None:
        return InvestigationOut(failure="MODEL_NOT_CONFIGURED")

    bundle = await _evidence_for(session, alert)
    facts = investigation.metric_facts_from_curves(bundle.curves)
    if not facts:
        return InvestigationOut(failure="NO_EVIDENCE")

    digest = (
        handling_digest.build_handling_digest(
            session,
            group_key=str(getattr(incident, "group_key", "") or ""),
            now=datetime.now(timezone.utc),
            current_started_at=getattr(incident, "occurrence_started_at", None)
            or alert.starts_at,
        )
        if incident is not None
        else None
    )
    payload = investigation.build_investigation_input(
        alertname=str((alert.labels or {}).get("alertname") or ""),
        labels=dict(alert.labels or {}),
        annotations=dict(alert.annotations or {}),
        started_at=alert.starts_at or utcnow(),
        metric_facts=facts,
        digest=digest,
    )

    try:
        outcome = await investigation.run_investigation(
            model=active.client(), investigation_input=payload
        )
    except ModelCallError as exc:
        _log_investigation(
            session, alert_id, source_id, active, ok=False, code=exc.kind.value
        )
        return InvestigationOut(
            failure=exc.kind.value, possibly_billed=exc.possibly_billed
        )

    _log_investigation(
        session,
        alert_id,
        source_id,
        active,
        ok=outcome.ok,
        code=outcome.failure or "OK",
        model_calls=outcome.model_calls,
        result={"snapshot": outcome.snapshot, "raw": outcome.raw_text},
    )
    if not outcome.ok or outcome.result is None:
        return InvestigationOut(
            failure=outcome.failure or "FAILED_VALIDATION",
            possibly_billed=True,
            model_calls=outcome.model_calls,
            model_name=outcome.model_name,
        )

    return InvestigationOut(
        hypotheses=[
            HypothesisOut(
                statement=item.statement,
                verdict=item.verdict,
                supporting_fact_ids=list(item.supporting_fact_ids),
                contradicting_fact_ids=list(item.contradicting_fact_ids),
                missing_evidence=list(item.missing_evidence),
                recommendations=[
                    RecommendationOut(kind=rec.kind, text=rec.text)
                    for rec in item.recommendations
                ],
            )
            for item in outcome.result.hypotheses
        ],
        facts=[FactOut(fact_id=f.fact_id, statement=f.statement) for f in facts]
        + [
            FactOut(fact_id=str(item["fact_id"]), statement=str(item["statement"]))
            for item in payload.history_facts
        ],
        possibly_billed=True,
        model_calls=outcome.model_calls,
        model_name=outcome.model_name,
        prompt_version=outcome.prompt_version,
    )


async def _evidence_for(session: Session, alert: Alert) -> MetricEvidenceOut:
    """Reuse the deterministic evidence endpoint instead of duplicating it."""
    from app.api import metrics

    alert_id = int(alert.id or 0)
    return await metrics.get_alert_metric_evidence(
        alert_id, window_mode="RECENT", session=session
    )


def _log_investigation(
    session: Session,
    alert_id: int,
    source_id: str,
    active: ActiveModel,
    *,
    ok: bool,
    code: str,
    model_calls: int = 1,
    result: dict[str, Any] | None = None,
) -> None:
    session.add(
        ModelCallLog(
            purpose="INVESTIGATE",
            alert_id=alert_id,
            source_scope=source_id,
            model_channel_id=active.channel_id,
            model_name=active.model,
            ok=ok,
            code=code,
            model_calls=model_calls,
            result_json=json.dumps(result or {}, ensure_ascii=False)[:200_000],
        )
    )
    session.commit()
