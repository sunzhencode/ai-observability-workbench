"""Alert endpoint: raw/normalized alert detail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.models import Alert
from app.schemas import AlertOut
from app.services.source_scope import visible_source_ids

router = APIRouter()


@router.get("/alerts/{alert_id}", response_model=AlertOut)
def get_alert(alert_id: int, session: Session = Depends(get_session)) -> AlertOut:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    # Same rule as incident detail: a directly requested source stays reachable
    # even when disabled or archived, so links into history keep working. F21
    # removed the `.env` comparison this used to make -- with no active `.env`
    # source left, it would have 404'd every alert.
    if alert.source_id not in visible_source_ids(
        session, requested=[alert.source_id], include_archived=True
    ):
        raise HTTPException(status_code=404, detail="Alert not found")
    payload = alert.model_dump()
    payload["fingerprint"] = alert.upstream_fingerprint or alert.fingerprint
    return AlertOut.model_validate(payload)
