"""Configuring a source's Grafana address, and importing dashboards from it.

Five routes, and one property runs through all of them: **preview never writes**
(CAP-13.2, the rule aggregation rule previews already follow). Reading a
dashboard produces a proposal on screen; only `confirm` stores anything, and
only what was ticked.

The "test before import" gate lives on dashboard listing, preview and confirm:
an untested address returns 409. It exists because there is a stored credential
here, and a first failure discovered halfway through an import is a bad place to
learn about it — not because a Grafana address is dangerous. On screen it is
only a disabled button and a hint; there is no draft state and no activation
step, and none of that vocabulary belongs on the data-source side (review Q5).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config_schemas import (
    EventSourceGrafanaIn,
    EventSourceOut,
    GrafanaDashboardSummaryOut,
    GrafanaImportConfirmIn,
    GrafanaImportConfirmOut,
    GrafanaImportPreviewIn,
    GrafanaImportPreviewOut,
    ImportCandidateOut,
    ImportPendingVariableOut,
    ImportSubstitutionOut,
    TestResultOut,
)
from app.crypto import LazySecretBox, SecretBox, SecretError
from app.db import get_session
from app.registry_models import (
    EventSource,
    MetricQueryTemplate,
    MetricTemplateOrigin,
    SourceGrafanaConfig,
)
from app import master_key as master_key_module
from app.services import event_sources as event_sources_service
from app.services import grafana_import
from app.services.grafana_import import (
    Candidate,
    ConfirmItem,
    ConfirmOrigin,
    DiffKind,
)
from app.services.metric_templates import MAX_AUXILIARY_CURVES
from app.services.thanos_history import enabled_thanos_connections
from app.sources.grafana_dashboards import GrafanaDashboardClient, GrafanaError
from app.sources.thanos import ThanosClient

router = APIRouter()


def _source(session: Session, source_id: str) -> EventSource:
    source = session.get(EventSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Event source not found")
    return source


def _config(session: Session, source_id: str) -> SourceGrafanaConfig:
    row = event_sources_service.grafana_config_row(session, source_id)
    if row is None or not row.base_url:
        raise HTTPException(
            status_code=409, detail="这个来源还没有配置 Grafana 地址"
        )
    return row


def _client(session: Session, row: SourceGrafanaConfig) -> GrafanaDashboardClient:
    token = ""
    if row.secret_envelope_json is not None:
        try:
            token = SecretBox(master_key_module.master_key()).decrypt(
                row.secret_envelope_json
            )
        except SecretError:
            raise HTTPException(
                status_code=503, detail="本地主密钥不可用，无法读取已保存的凭证"
            ) from None
    return GrafanaDashboardClient(
        base_url=row.base_url,
        token=token,
        timeout_seconds=row.timeout_seconds,
    )


def _require_tested(row: SourceGrafanaConfig) -> None:
    """The gate. A 409 rather than a 403: nothing is forbidden, the
    configuration is simply not ready to be used yet."""

    if row.last_test_status != "OK":
        raise HTTPException(
            status_code=409,
            detail="请先测试 Grafana 连接，测试成功后才能导入",
        )


def _thanos_for(session: Session, source_id: str) -> ThanosClient | None:
    for connection in enabled_thanos_connections(session):
        if connection.source_id == source_id and connection.usable:
            return ThanosClient(
                base_url=connection.base_url,
                token=connection.token,
                timeout=connection.timeout_seconds,
            )
    return None


@router.put(
    "/event-sources/{source_id}/grafana", response_model=EventSourceOut
)
def save_grafana_config(
    source_id: str,
    payload: EventSourceGrafanaIn,
    session: Session = Depends(get_session),
) -> EventSourceOut:
    source = _source(session, source_id)
    if source.lifecycle_state == "ARCHIVED":
        raise HTTPException(status_code=409, detail="archived event source is read-only")
    try:
        event_sources_service.write_grafana_config(
            session,
            source_id,
            {
                "url": payload.url,
                "secret_action": payload.secret.action,
                "secret_value": payload.secret.value,
                "timeout_seconds": payload.timeout_seconds,
            },
            box=LazySecretBox(lambda: master_key_module.master_key()),
        )
    except SecretError:
        raise HTTPException(status_code=503, detail="secret key is unavailable") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    session.refresh(source)
    return EventSourceOut.model_validate(
        event_sources_service.event_source_public_dict(session, source)
    )


@router.post(
    "/event-sources/{source_id}/grafana/test", response_model=TestResultOut
)
async def test_grafana_config(
    source_id: str, session: Session = Depends(get_session)
) -> TestResultOut:
    """One read-only listing request. Explicit, and the only thing that opens
    the import gate."""

    _source(session, source_id)
    row = _config(session, source_id)
    client = _client(session, row)
    try:
        await client.search_dashboards("", limit=1)
    except GrafanaError as exc:
        row.last_test_status = "FAILED"
        row.safe_error_code = exc.code
        row.tested_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
        # The submitted values stay on the form; a failed test must not cost the
        # user what they typed (CAP-01.7).
        return TestResultOut(ok=False, code=exc.code)
    row.last_test_status = "OK"
    row.safe_error_code = None
    row.tested_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    return TestResultOut(ok=True, code="OK")


@router.get(
    "/event-sources/{source_id}/grafana/dashboards",
    response_model=list[GrafanaDashboardSummaryOut],
)
async def list_grafana_dashboards(
    source_id: str, query: str = "", session: Session = Depends(get_session)
) -> list[GrafanaDashboardSummaryOut]:
    _source(session, source_id)
    row = _config(session, source_id)
    _require_tested(row)
    client = _client(session, row)
    try:
        rows = await client.search_dashboards(query, limit=50)
    except GrafanaError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from None
    return [
        GrafanaDashboardSummaryOut(
            uid=str(item.get("uid") or ""),
            title=str(item.get("title") or ""),
            folder_title=str(item.get("folderTitle") or ""),
        )
        for item in rows
        if item.get("uid")
    ]


@router.post(
    "/event-sources/{source_id}/grafana/import/preview",
    response_model=GrafanaImportPreviewOut,
)
async def preview_grafana_import(
    source_id: str,
    payload: GrafanaImportPreviewIn,
    session: Session = Depends(get_session),
) -> GrafanaImportPreviewOut:
    """Read, parse, diff and check — and write nothing.

    There is no server-side draft to store: the candidates go to the browser and
    come back as literals on confirmation. That is also why this function
    contains no `session.add`, `flush` or `commit`, which a test asserts by
    counting rows before and after.
    """

    _source(session, source_id)
    row = _config(session, source_id)
    _require_tested(row)
    client = _client(session, row)
    try:
        dashboard = await client.get_dashboard(payload.dashboard_uid)
    except GrafanaError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from None

    candidates = grafana_import.parse_dashboard(dashboard)

    origins = list(
        session.exec(
            select(MetricTemplateOrigin).where(
                MetricTemplateOrigin.source_id == source_id,
                MetricTemplateOrigin.dashboard_uid == payload.dashboard_uid,
            )
        ).all()
    )
    templates = (
        list(
            session.exec(
                select(MetricQueryTemplate).where(
                    MetricQueryTemplate.id.in_(
                        [origin.template_id for origin in origins]
                    )
                )
            ).all()
        )
        if origins
        else []
    )

    thanos = _thanos_for(session, source_id)
    probed = await grafana_import.probe_candidates(candidates, thanos)

    entries = grafana_import.diff_candidates(probed, origins, templates)
    enabled_count = len(
        session.exec(
            select(MetricQueryTemplate).where(MetricQueryTemplate.enabled.is_(True))
        ).all()
    )

    return GrafanaImportPreviewOut(
        dashboard_uid=payload.dashboard_uid,
        dashboard_title=str(dashboard.get("title") or ""),
        candidates=[_entry_out(entry) for entry in entries],
        probed=thanos is not None,
        enabled_template_count=enabled_count,
        max_auxiliary_curves=MAX_AUXILIARY_CURVES,
    )


def _candidate_out(candidate: Candidate) -> ImportCandidateOut:
    return ImportCandidateOut(
        panel_id=candidate.panel_id,
        panel_title=candidate.panel_title,
        ref_id=candidate.ref_id,
        raw_promql=candidate.raw_promql,
        resolved_promql=candidate.resolved_promql,
        status=candidate.status.value,
        suggested_name=candidate.suggested_name,
        order=candidate.order,
        reason=candidate.reason,
        substitutions=[
            ImportSubstitutionOut(macro=item.macro, replacement=item.replacement)
            for item in candidate.substitutions
        ],
        pending_variables=[
            ImportPendingVariableOut(
                name=item.name,
                suggestion=item.suggestion,
                sample_value=item.sample_value,
                multi=item.multi,
            )
            for item in candidate.pending_variables
        ],
        display_unit=candidate.display_unit,
        probe_value=candidate.probe_value,
        probe_note=candidate.probe_note,
    )


def _entry_out(entry) -> ImportCandidateOut:
    if entry.candidate is None:
        # A target Grafana no longer has. Nothing is deleted for it — removing a
        # template is the user's own action on the template page.
        return ImportCandidateOut(
            panel_id=entry.panel_id,
            panel_title=entry.panel_title,
            ref_id=entry.ref_id,
            raw_promql="",
            resolved_promql="",
            status="UNSUPPORTED",
            suggested_name="",
            order=10_000,
            reason="这条在 Grafana 上已经不存在了。已导入的模板保持原样，"
            "要删除请到指标模板页手动删。",
            diff_kind=DiffKind.GONE.value,
            template_id=entry.template_id,
            imported_promql=entry.imported_promql,
            current_promql=entry.current_promql,
        )
    out = _candidate_out(entry.candidate)
    return out.model_copy(
        update={
            "diff_kind": entry.kind.value,
            "template_id": entry.template_id,
            "imported_promql": entry.imported_promql,
            "current_promql": entry.current_promql,
        }
    )


@router.post(
    "/event-sources/{source_id}/grafana/import/confirm",
    response_model=GrafanaImportConfirmOut,
)
def confirm_grafana_import(
    source_id: str,
    payload: GrafanaImportConfirmIn,
    session: Session = Depends(get_session),
) -> GrafanaImportConfirmOut:
    """The only writing route. One transaction for the whole batch."""

    _source(session, source_id)
    config = _config(session, source_id)
    _require_tested(config)
    items = [
        ConfirmItem(
            final_promql=item.final_promql,
            imported_promql=item.imported_promql,
            name=item.name,
            origin=ConfirmOrigin(
                dashboard_uid=item.origin.dashboard_uid,
                dashboard_title=item.origin.dashboard_title,
                panel_id=item.origin.panel_id,
                panel_title=item.origin.panel_title,
                ref_id=item.origin.ref_id,
            ),
            required_labels=tuple(item.required_labels),
            enabled=item.enabled,
            display_unit=item.display_unit,
            order=item.order,
            template_id=item.template_id,
        )
        for item in payload.items
    ]
    try:
        result = grafana_import.confirm_import(session, source_id, items)
        session.commit()
    except IntegrityError:
        session.rollback()
        # The same panel target was confirmed twice — a double click, or two
        # tabs. The database refused it, which is the point of the constraint.
        raise HTTPException(
            status_code=409, detail="这些 panel 已经导入过了，没有重复写入"
        ) from None
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return GrafanaImportConfirmOut(
        created_template_ids=result.created_template_ids,
        updated_template_ids=result.updated_template_ids,
    )
