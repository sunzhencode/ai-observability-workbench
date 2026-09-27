"""F27 metric evidence (CAP-12) and the query-template catalogue.

This module is the **boundary**: it resolves the Thanos connection, issues the
bounded reads, and hands what came back to `services/metric_evidence.py`, which
is pure. Nothing here decides a tier or classifies a metric.

Two properties are load-bearing and easy to lose:

- **Evidence belongs to one Alert, never to an Incident** (D28). Members of a
  group share their grouping labels and nothing else — the grouping key cannot
  even contain `alertname` — so "pick a representative member" made the chart
  depend on which member was polled last.
- **A degraded curve is not a missing curve** (D39). Falling back to the alert's
  link, guessing a type from a suffix, or guessing a label schema all produce a
  real chart; pairing it with "upstream unreachable" misleads the reader.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.db import get_session
from app.registry_models import MetricQueryTemplate
from app.models import Alert, Incident, utcnow
from app.schemas import (
    CurveOut,
    CurveSeriesOut,
    EvidenceNoteOut,
    MetricEvidenceOut,
    MetricTemplateCreate,
    MetricTemplateOriginOut,
    MetricTemplateOut,
    MetricTemplateUpdate,
)
from app.services import (
    metric_cache,
    metric_evidence,
    metric_templates,
    promql,
)
from app.services.metric_budget import (
    MAX_SERIES_PER_QUERY,
    BudgetExceeded,
    assert_query_count_within_budget,
    assert_series_count_within_budget,
    assert_window_within_budget,
    resolve_window,
)
from app.services.metric_evidence import (
    EvidenceFailureKind,
    EvidenceNote,
    EvidenceWarningKind,
    ExprOrigin,
    MetricType,
    Tier,
)
from app.services import evidence_context
from app.services.evidence_context import (
    SourceSnapshot,
    source_changed,
    source_snapshot,
    source_visible,
)
from app.sources.thanos import ThanosClient, safe_error_code

router = APIRouter()

# Only the primary curve plus its auxiliaries; `/series` prechecks use limit=21 so
# that "more than the ceiling" is detectable without fetching them all.
SERIES_PROBE_LIMIT = MAX_SERIES_PER_QUERY + 1


async def _alert_rules(
    client: ThanosClient, snapshot: SourceSnapshot, alertname: str
) -> tuple[list[dict[str, Any]], EvidenceNote | None]:
    """Rules for one alert name, cached per source *config version* + name.

    Keyed on the version because a source repointed at a different store must not
    keep answering from the previous one's rules (D38). Scoped to the alert name
    because D31 made this a targeted lookup rather than a whole-catalogue fetch.
    """

    key = f"{snapshot.cache_scope}|rules|{alertname}"
    hit, cached = metric_cache.RULES_CACHE.get(key)
    if hit:
        return cached, None
    try:
        rules = await client.alert_rules(alertname=alertname)
    except Exception as exc:  # noqa: BLE001 - classified, never re-raised raw
        # Not fatal: the generatorURL fallback is precisely why the source order
        # has two layers. A curve may still appear, so this is a warning.
        return [], EvidenceNote(
            kind=EvidenceWarningKind.RULES_ENDPOINT_UNAVAILABLE,
            subject="primary",
            detail=safe_error_code(exc),
        )
    metric_cache.RULES_CACHE.set(key, rules, metric_cache.rules_ttl_for(rules))
    return rules, None


async def _metadata(client: ThanosClient, snapshot: SourceSnapshot, metric: str):
    key = f"{snapshot.cache_scope}|meta|{metric}"
    hit, cached = metric_cache.METADATA_CACHE.get(key)
    if hit:
        return cached
    meta = await client.metric_metadata(metric)
    metric_cache.METADATA_CACHE.set(key, meta, metric_cache.METADATA_TTL_SECONDS)
    return meta


async def _label_names(client: ThanosClient, snapshot: SourceSnapshot, metric: str, window):
    key = f"{snapshot.cache_scope}|labels|{metric}"
    hit, cached = metric_cache.METADATA_CACHE.get(key)
    if hit:
        return cached
    names = await client.metric_label_names(metric, window.start, window.end)
    metric_cache.METADATA_CACHE.set(key, names, metric_cache.METADATA_TTL_SECONDS)
    return names


@router.get("/alerts/{alert_id}/metric-evidence", response_model=MetricEvidenceOut)
async def get_alert_metric_evidence(
    alert_id: int,
    window_mode: str = Query(default="RECENT", pattern="^(RECENT|ONSET)$"),
    session: Session = Depends(get_session),
) -> MetricEvidenceOut:
    """Evidence for **one** alert — see the module docstring on why not a group."""

    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    incident = (
        session.get(Incident, alert.incident_id) if alert.incident_id else None
    )
    source_id = str(
        getattr(incident, "source_id", "") or getattr(alert, "source_id", "") or ""
    )
    if source_id and not source_visible(session, source_id):
        raise HTTPException(status_code=404, detail="Alert not found")

    labels = dict(alert.labels or {})
    snapshot = source_snapshot(session, source_id)
    if not snapshot.usable:
        return _bundle_out(
            metric_evidence.build_bundle(
                alert_starts_at=alert.starts_at,
                curves=[],
                failures=[
                    EvidenceNote(
                        kind=EvidenceFailureKind.THANOS_NOT_CONFIGURED,
                        subject="primary",
                        detail="this source has no usable history address configured",
                    )
                ],
            )
        )

    selection = metric_templates.candidate_templates(
        session, labels, source_id=source_id
    )
    templates = selection.selected
    template_ids = [int(row.id or 0) for row in templates]
    template_extras_map = metric_templates.extras_for_many(session, template_ids)
    window = resolve_window(
        alert_starts_at=alert.starts_at or utcnow(),
        # `resolved` is the word on an **Alert** row; `recovered` is the derived
        # state on the Incident. Comparing against the latter here meant every
        # ended alert took the still-firing branch and charted up to now.
        alert_ends_at=alert.ends_at if alert.source_state == "resolved" else None,
        now=datetime.now(timezone.utc),
    )
    assert_window_within_budget(window)
    assert_query_count_within_budget(1 + len(templates))

    client = evidence_context.build_thanos_client(snapshot.connection)
    occurrence_no = int(getattr(incident, "occurrence_no", 1) or 1)
    queried_at = datetime.now(timezone.utc)

    curves: list[metric_evidence.Curve] = []
    warnings: list[EvidenceNote] = []
    failures: list[EvidenceNote] = []

    # --- primary -----------------------------------------------------------
    rules, rules_note = await _alert_rules(client, snapshot, alert.alertname)
    if rules_note is not None:
        warnings.append(rules_note)
    rule_expr, ambiguity = metric_evidence.rule_expression_for(
        rules, alert.alertname, alert_labels=labels
    )
    if ambiguity is not None:
        failures.append(
            EvidenceNote(
                kind=ambiguity,
                subject="primary",
                detail=(
                    "several rules share this alert name and the alert's labels "
                    "do not single one out"
                ),
            )
        )
    else:
        generator_url = str((alert.raw_payload or {}).get("generatorURL") or "")
        plan, plan_failure = metric_evidence.plan_primary(
            rule_expr=rule_expr, generator_url=generator_url
        )
        if plan_failure is not None:
            failures.append(plan_failure)
        else:
            if plan.expr_origin is ExprOrigin.GENERATOR_URL:
                warnings.append(
                    EvidenceNote(
                        kind=EvidenceWarningKind.EXPR_FROM_GENERATOR_URL,
                        subject="primary",
                        detail="reconstructed from the alert's link, not the rule",
                    )
                )
            curve, notes = await _primary_curve(
                client=client,
                snapshot=snapshot,
                plan=plan,
                labels=labels,
                alert=alert,
                occurrence_no=occurrence_no,
                window=window,
                window_mode=window_mode,
                queried_at=queried_at,
            )
            if curve is not None:
                curves.append(curve)
            for note in notes:
                (warnings if isinstance(note.kind, EvidenceWarningKind) else failures).append(note)

    # --- auxiliary ---------------------------------------------------------
    if selection.omitted:
        # D40: never drop a curve the reader deliberately enabled without saying so.
        warnings.append(
            EvidenceNote(
                kind=EvidenceWarningKind.SERIES_TRUNCATED_FOR_DISPLAY,
                subject="auxiliary",
                detail=(
                    f"{selection.matched} templates matched; "
                    f"{selection.omitted} were not shown (priority order)"
                ),
            )
        )
    for template in templates:
        rendered = metric_templates.render(template, labels)
        if rendered is None:
            failures.append(
                EvidenceNote(
                    kind=EvidenceFailureKind.EXPR_UNPARSEABLE,
                    subject=template.name,
                    detail="a placeholder had no matching label on this alert",
                )
            )
            continue
        curve, note = await _run_curve(
            client=client,
            alert=alert,
            snapshot=snapshot,
            occurrence_no=occurrence_no,
            window=window,
            window_mode=window_mode,
            queried_at=queried_at,
            kind="AUXILIARY",
            title=template.name,
            query=rendered,
            expr_origin=ExprOrigin.TEMPLATE,
            # Template PromQL is authored whole; the system never rewrites it, so
            # there is no tier and the type is only a label.
            tier=None,
            metric_type=MetricType.UNKNOWN,
            threshold=None,
            threshold_operator=None,
            subject=template.name,
            display_unit=template_extras_map.get(
                int(template.id or 0), metric_templates.TemplateExtras()
            ).display_unit,
        )
        if curve is not None:
            curves.append(curve)
        if note is not None:
            # An auxiliary that simply did not apply reports as a warning; a real
            # fault on the way to it (unreachable, over budget) still shouts.
            if note.kind is EvidenceWarningKind.AUXILIARY_NO_DATA:
                warnings.append(note)
            else:
                failures.append(note)

    # --- the source must still be the one we read (D38) --------------------
    if source_changed(session, snapshot):
        return _bundle_out(
            metric_evidence.build_bundle(
                alert_starts_at=alert.starts_at,
                curves=[],
                failures=[
                    EvidenceNote(
                        kind=EvidenceFailureKind.SOURCE_CONFIG_CHANGED,
                        subject="primary",
                        detail="the source was changed while this evidence was read",
                    )
                ],
            )
        )

    return _bundle_out(
        metric_evidence.build_bundle(
            alert_starts_at=alert.starts_at,
            curves=curves,
            warnings=warnings,
            failures=failures,
        )
    )


async def _primary_curve(
    *, client, snapshot, plan, labels, alert, occurrence_no, window, window_mode, queried_at
):
    """Compose and run the primary curve, collecting its warnings on the way."""

    notes: list[EvidenceNote] = []
    metadata = None
    allowed_labels = None

    if plan.tier is Tier.METRIC:
        try:
            metadata = await _metadata(client, snapshot, plan.metric)
        except Exception as exc:  # noqa: BLE001
            # The type decision degrades to a suffix heuristic; the curve still
            # appears, so this is a warning rather than a failure.
            notes.append(
                EvidenceNote(
                    kind=EvidenceWarningKind.METRIC_TYPE_UNKNOWN,
                    subject="primary",
                    detail=safe_error_code(exc),
                )
            )
        try:
            allowed_labels = set(
                await _label_names(client, snapshot, plan.metric, window)
            )
        except Exception:  # noqa: BLE001
            # D35: without the real schema we fall back to the fixed allowlist,
            # which drops exporter-specific labels. Say so rather than pretend.
            allowed_labels = None
            notes.append(
                EvidenceNote(
                    kind=EvidenceWarningKind.LABEL_SCHEMA_GUESSED,
                    subject="primary",
                    detail="the metric's label schema was unavailable",
                )
            )

    metric_type = (
        metric_evidence.classify_metric_type(plan.metric, metadata)
        if plan.tier is Tier.METRIC
        else MetricType.UNKNOWN
    )
    if metric_type is MetricType.UNKNOWN_SUFFIX_GUESS:
        notes.append(
            EvidenceNote(
                kind=EvidenceWarningKind.METRIC_TYPE_UNKNOWN,
                subject="primary",
                detail="type inferred from the metric name suffix",
            )
        )
    if plan.tier in (Tier.THRESHOLD, Tier.EXPRESSION_RESULT) and any(
        plan.source_expression.endswith(suffix) or f"{suffix}" in plan.expression
        for suffix in ("_total",)
    ):
        # D34: the author compared a cumulative counter directly. We run it as
        # written — wrapping a threshold in rate() would compare unrelated units.
        notes.append(
            EvidenceNote(
                kind=EvidenceWarningKind.COUNTER_AS_AUTHORED,
                subject="primary",
                detail="the rule compares a cumulative counter; executed as authored",
            )
        )

    query = metric_evidence.finalize_primary(
        plan, labels, metric_type, allowed_label_names=allowed_labels
    )
    curve, note = await _run_curve(
        client=client,
        alert=alert,
        snapshot=snapshot,
        occurrence_no=occurrence_no,
        window=window,
        window_mode=window_mode,
        queried_at=queried_at,
        kind="PRIMARY",
        title=metric_evidence.curve_title(plan, metadata),
        query=query,
        expr_origin=plan.expr_origin,
        tier=plan.tier,
        metric_type=metric_type,
        threshold=plan.threshold,
        threshold_operator=plan.threshold_operator,
        subject="primary",
        metric=plan.metric,
        display_unit=str((metadata or {}).get("unit") or ""),
    )
    if note is not None:
        notes.append(note)
    return curve, notes


async def _run_curve(
    *,
    client: ThanosClient,
    alert: Alert,
    snapshot: SourceSnapshot,
    occurrence_no: int,
    window,
    window_mode: str,
    queried_at: datetime,
    kind: str,
    title: str,
    query: str,
    expr_origin: ExprOrigin,
    tier,
    metric_type: MetricType,
    threshold: float | None,
    threshold_operator: str | None,
    subject: str,
    metric: str = "",
    display_unit: str = "",
) -> tuple[metric_evidence.Curve | None, EvidenceNote | None]:
    """One bounded read plus its classification. Never raises."""

    identifier = metric_evidence.curve_id(
        alert_fingerprint=str(alert.fingerprint or alert.id or ""),
        occurrence_no=occurrence_no,
        source_config_version=snapshot.version,
        kind=kind,
        query=query,
        window_mode=window_mode,
        window_start=window.start,
        window_end=window.end,
    )

    # D37: the outer window says nothing about what the expression itself scans.
    try:
        promql.assert_query_scope_safe(query)
    except promql.QueryScopeUnsafe as exc:
        return None, EvidenceNote(
            kind=EvidenceFailureKind.QUERY_SCOPE_UNSAFE, subject=subject, detail=str(exc)
        )

    try:
        payload = await client.query_range(
            query, window.start, window.end, window.step_seconds
        )
    except BudgetExceeded as exc:
        return None, EvidenceNote(
            kind=EvidenceFailureKind.BUDGET_EXCEEDED,
            subject=subject,
            detail=str(exc.reason),
        )
    except Exception as exc:  # noqa: BLE001
        return None, EvidenceNote(
            kind=EvidenceFailureKind.THANOS_UNREACHABLE,
            subject=subject,
            detail=safe_error_code(exc),
        )

    try:
        curve = metric_evidence.curve_from_matrix(
            curve_id_value=identifier,
            kind=kind,
            title=title,
            query=query,
            expr_origin=expr_origin,
            tier=tier,
            metric_type=metric_type,
            threshold=threshold,
            threshold_operator=threshold_operator,
            display_unit=display_unit,
            window_mode=window_mode,
            queried_at=queried_at,
            window=window,
            payload=payload,
        )
    except BudgetExceeded as exc:
        return None, EvidenceNote(
            kind=EvidenceFailureKind.SERIES_LIMIT_EXCEEDED,
            subject=subject,
            detail=exc.detail,
        )

    if curve.is_empty:
        # D30: four different reasons, four different next steps. Only claim the
        # metric is missing when its absence can actually be proven.
        known_metrics = None
        series_found = 0
        if tier is Tier.METRIC and metric:
            try:
                known_metrics = set(
                    await client.metric_names(window.start, window.end)
                )
            except Exception:  # noqa: BLE001
                known_metrics = None
            try:
                series_found = len(
                    await client.series(
                        [query], window.start, window.end, limit=SERIES_PROBE_LIMIT
                    )
                )
            except Exception:  # noqa: BLE001
                series_found = 0
        reason = metric_evidence.classify_empty_result(
            tier=tier,
            metric=metric,
            known_metrics=known_metrics,
            series_found=series_found,
        )
        if kind == "AUXILIARY":
            # Not a failure: `required_labels` only checks that a label is
            # *present*, not that it means the same kind of thing. An ES exporter
            # alert carries `instance`, so a node template matches and then finds
            # nothing — the template simply does not apply to this alert.
            return None, EvidenceNote(
                kind=EvidenceWarningKind.AUXILIARY_NO_DATA,
                subject=subject,
                detail=reason.value,
            )
        return None, EvidenceNote(
            kind=reason,
            subject=subject,
            detail="the query returned no samples in this window",
        )
    return curve, None


def _bundle_out(bundle: metric_evidence.EvidenceBundle) -> MetricEvidenceOut:
    return MetricEvidenceOut(
        alert_starts_at=bundle.alert_starts_at,
        curves=[
            CurveOut(
                curve_id=curve.curve_id,
                kind=curve.kind,
                title=curve.title,
                query=curve.query,
                expr_origin=curve.expr_origin.value,
                tier=curve.tier.value if curve.tier else None,
                metric_type=curve.metric_type.value,
                threshold=curve.threshold,
                threshold_operator=curve.threshold_operator,
                display_unit=curve.display_unit,
                window_mode=curve.window_mode,
                queried_at=curve.queried_at,
                window_start=curve.window_start,
                window_end=curve.window_end,
                step_seconds=curve.step_seconds,
                series=[
                    CurveSeriesOut(
                        labels=item.labels,
                        points=[[float(ts), value] for ts, value in item.points],
                    )
                    for item in curve.series
                ],
            )
            for curve in bundle.curves
        ],
        warnings=[_note_out(note) for note in bundle.warnings],
        failures=[_note_out(note) for note in bundle.failures],
    )


def _note_out(note: EvidenceNote) -> EvidenceNoteOut:
    return EvidenceNoteOut(
        kind=note.kind.value, subject=note.subject, detail=str(note.detail)
    )


# ---------------------------------------------------------------------------
# template catalogue
# ---------------------------------------------------------------------------


def _template_out(
    row: MetricQueryTemplate,
    session: Session,
    *,
    origins: dict[int, metric_templates.TemplateOrigin] | None = None,
    scopes: dict[int, dict[str, object]] | None = None,
) -> MetricTemplateOut:
    extras = metric_templates.extras_for(session, row.id)
    template_id = int(row.id or 0)
    if origins is None:
        origins = metric_templates.origins_for(session, [template_id])
    if scopes is None:
        scopes = metric_templates.template_scopes_for(session, [template_id])
    origin = origins.get(template_id)
    return MetricTemplateOut(
        id=template_id,
        priority=extras.priority,
        display_unit=extras.display_unit,
        name=row.name,
        promql=row.promql,
        required_labels=list(metric_templates.required_labels(row)),
        description=row.description,
        builtin_key=row.builtin_key,
        user_modified=row.user_modified,
        enabled=row.enabled,
        origin=(
            MetricTemplateOriginOut(
                source_id=origin.source_id,
                dashboard_uid=origin.dashboard_uid,
                dashboard_title=origin.dashboard_title,
                panel_id=origin.panel_id,
                panel_title=origin.panel_title,
            )
            if origin is not None
            else None
        ),
        source_scope=scopes.get(
            template_id, {"mode": "ALL", "source_ids": []}
        ),
    )


@router.get("/metric-templates", response_model=list[MetricTemplateOut])
def list_metric_templates(
    session: Session = Depends(get_session),
) -> list[MetricTemplateOut]:
    rows = session.exec(
        select(MetricQueryTemplate).order_by(
            MetricQueryTemplate.name, MetricQueryTemplate.id
        )
    ).all()
    # Fetched in one query each rather than per row: after an import the list is
    # forty entries long, not eight.
    ids = [int(row.id or 0) for row in rows]
    origins = metric_templates.origins_for(session, ids)
    scopes = metric_templates.template_scopes_for(session, ids)
    return [
        _template_out(
            row, session, origins=origins, scopes=scopes
        )
        for row in rows
    ]


def _assert_template_query_safe(query: str) -> str:
    """D37: a user template is checked **when saved**, not when it is charted.

    Saving `rate(x[30d])` used to succeed and then fail at chart time as one red
    line among several — the author learns about it later, somewhere else, on a
    screen that does not offer an edit box. The requirement exists precisely to
    move that discovery back to the moment of typing.

    The message never quotes the input: `str(ValidationError)` carrying a user
    string back out is how a paste of something sensitive ends up in a response
    body (F24's lesson).
    """
    cleaned = str(query or "").strip()
    try:
        promql.assert_query_scope_safe(cleaned)
    except promql.QueryScopeUnsafe:
        raise HTTPException(
            status_code=422,
            detail=(
                "这条查询的时间范围超出护栏：区间选择器上限 6 小时，"
                "且不允许 offset / @ / 子查询"
            ),
        ) from None
    return cleaned


@router.post("/metric-templates", response_model=MetricTemplateOut, status_code=201)
def create_metric_template(
    payload: MetricTemplateCreate, session: Session = Depends(get_session)
) -> MetricTemplateOut:
    row = MetricQueryTemplate(
        name=payload.name.strip(),
        promql=_assert_template_query_safe(payload.promql),
        required_labels_json=json.dumps(
            [label.strip() for label in payload.required_labels if label.strip()]
        ),
        description=payload.description.strip(),
        builtin_key=None,
        user_modified=False,
        enabled=False,
    )
    session.add(row)
    session.flush()
    metric_templates.set_template_extras(
        session,
        int(row.id or 0),
        priority=payload.priority,
        display_unit=payload.display_unit,
    )
    try:
        metric_templates.set_template_scope(
            session, int(row.id or 0), payload.source_scope.model_dump()
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    session.refresh(row)
    return _template_out(row, session)


@router.patch("/metric-templates/{template_id}", response_model=MetricTemplateOut)
def update_metric_template(
    template_id: int,
    payload: MetricTemplateUpdate,
    session: Session = Depends(get_session),
) -> MetricTemplateOut:
    row = session.get(MetricQueryTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Metric template not found")

    content_changed = False
    if payload.name is not None:
        row.name = payload.name.strip()
        content_changed = True
    if payload.promql is not None:
        # The edit path is the same door; checking only on create leaves a way round.
        row.promql = _assert_template_query_safe(payload.promql)
        content_changed = True
    if payload.required_labels is not None:
        row.required_labels_json = json.dumps(
            [label.strip() for label in payload.required_labels if label.strip()]
        )
        content_changed = True
    if payload.description is not None:
        row.description = payload.description.strip()
        content_changed = True
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.priority is not None or payload.display_unit is not None:
        metric_templates.set_template_extras(
            session,
            int(row.id or 0),
            priority=payload.priority,
            display_unit=payload.display_unit,
        )
    if payload.source_scope is not None:
        try:
            metric_templates.set_template_scope(
                session, int(row.id or 0), payload.source_scope.model_dump()
            )
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from None

    # Toggling a shipped template on or off is not "editing" it: doing so would
    # freeze it against every future correction to its query. Only a content
    # change claims ownership.
    if content_changed and row.builtin_key:
        row.user_modified = True

    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _template_out(row, session)


@router.delete("/metric-templates/{template_id}", status_code=204)
def delete_metric_template(
    template_id: int, session: Session = Depends(get_session)
) -> None:
    row = session.get(MetricQueryTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Metric template not found")
    if row.builtin_key:
        # Deleting one would only make the next startup seed it again, switched
        # off — the same state as disabling it, reached confusingly.
        raise HTTPException(
            status_code=409,
            detail="Built-in templates cannot be deleted; disable it instead",
        )
    # The side tables carry `ON DELETE CASCADE`, but that only fires where the
    # SQLite `foreign_keys` pragma is on. Doing it explicitly as well means the
    # current side rows go regardless of how the engine was built.
    metric_templates.delete_template_side_rows(session, int(row.id or 0))
    session.delete(row)
    session.commit()
