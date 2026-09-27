"""Local runtime settings.

F21 retired the ConnectionProfile endpoints that used to live here: data source
addresses and credentials are configured through the registry
(``/api/event-sources``; Thanos is a field on the source), and the one-time
`.env` import became migration v6 rather than an API. What remains is the
workbench base URL -- a non-secret local runtime value.
See docs/adr/0004-registry-only-configuration.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.config_schemas import WorkbenchUrlIn, WorkbenchUrlOut
from app.db import get_session
from app.models import RuntimeSetting

router = APIRouter()


def _validate_workbench_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("workbench URL must be an absolute HTTP(S) base URL")
    return url


@router.get("/settings/workbench-url", response_model=WorkbenchUrlOut)
def get_workbench_url(session: Session = Depends(get_session)) -> WorkbenchUrlOut:
    row = session.get(RuntimeSetting, "workbench_url")
    return WorkbenchUrlOut(url=str(row.value_json or "") if row else "")


@router.put("/settings/workbench-url", response_model=WorkbenchUrlOut)
def put_workbench_url(
    payload: WorkbenchUrlIn,
    session: Session = Depends(get_session),
) -> WorkbenchUrlOut:
    try:
        value = _validate_workbench_url(payload.url)
        row = session.get(RuntimeSetting, "workbench_url") or RuntimeSetting(
            key="workbench_url"
        )
        row.value_json = value
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        return WorkbenchUrlOut(url=value)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
