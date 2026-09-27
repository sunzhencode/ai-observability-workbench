"""Local panel mapping endpoints (scheme B)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlmodel import Session, select

from app.db import get_session
from app.models import PanelMapping
from app.schemas import PanelMappingOut, PanelMappingsOut, PanelMappingPut

router = APIRouter()


@router.get("/panel-mappings", response_model=PanelMappingsOut)
def list_panel_mappings(session: Session = Depends(get_session)) -> PanelMappingsOut:
    rows = session.exec(
        select(PanelMapping).order_by(PanelMapping.alertname, PanelMapping.id)
    ).all()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.alertname, []).append(row.url)
    return PanelMappingsOut(
        mappings=[
            PanelMappingOut(alertname=alertname, links=links)
            for alertname, links in grouped.items()
        ]
    )


@router.put(
    "/panel-mappings",
    response_model=PanelMappingOut,
    responses={204: {"description": "Mapping deleted or already absent"}},
)
def put_panel_mapping(
    payload: PanelMappingPut,
    session: Session = Depends(get_session),
) -> PanelMappingOut | Response:
    existing = session.exec(
        select(PanelMapping).where(PanelMapping.alertname == payload.alertname)
    ).all()

    for row in existing:
        session.delete(row)

    if not payload.links:
        session.commit()
        return Response(status_code=204)

    for index, url in enumerate(payload.links, start=1):
        session.add(
            PanelMapping(
                alertname=payload.alertname,
                url=url,
                label=f"Mapped link {index}",
            )
        )
    session.commit()
    return PanelMappingOut(alertname=payload.alertname, links=payload.links)
