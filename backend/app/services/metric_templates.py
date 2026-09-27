"""F27 auxiliary curves: the built-in catalogue, matching, and rendering.

Deterministic and offline — this module takes an already-open session and an
already-fetched label map, never a network client.

Auxiliary curves are the *secondary* half of the evidence (ADR 0009). The user's
own alerts span exporters well outside kube-prometheus, so a built-in catalogue
can never reach the coverage of the primary curve, which is derived from the
alert's own expression and needs no configuration at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session, select

from app.registry_models import (
    MetricQueryTemplate,
    MetricTemplateExtras,
    MetricTemplateOrigin,
    MetricTemplateSourceScope,
)
from app.domains.metrics.catalog import BUILTIN_TEMPLATES, BuiltinTemplate
from app.models import utcnow
from app.services.metric_budget import MAX_QUERIES_PER_ALERT
from app.services.promql import escape_label_value
from app.services.source_scope import (
    scope_contains,
    source_scope_dict,
    validate_source_scope,
)

# One slot is always reserved for the primary curve.
MAX_AUXILIARY_CURVES = MAX_QUERIES_PER_ALERT - 1

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Lower runs first, ties broken by id — the same convention aggregation rules and
# notification policies already use, so there is one ordering rule in the product.
DEFAULT_TEMPLATE_PRIORITY = 100


@dataclass(frozen=True)
class TemplateExtras:
    """Ordering and display, kept in a side table because v11 froze the model."""

    priority: int = DEFAULT_TEMPLATE_PRIORITY
    display_unit: str = ""


@dataclass(frozen=True)
class CandidateSelection:
    """What was chosen, and **what did not fit**.

    The omitted count is not decoration: the first version sorted by name and
    silently kept the first few, so a template the reader had deliberately
    enabled could vanish with nothing said. Silent truncation is the same defect
    class as F25's lying counters.
    """

    selected: list[MetricQueryTemplate]
    matched: int
    omitted: int


def _table_available(session: Session) -> bool:
    """Ask over the session's own connection.

    Inspecting the *engine* checks a connection out of the pool; against a
    StaticPool that is the very connection this session is using, and the
    inspector's rollback then discards whatever the caller had flushed.
    """

    return sa_inspect(session.connection()).has_table("metricquerytemplate")


def required_labels(template: MetricQueryTemplate) -> tuple[str, ...]:
    try:
        parsed = json.loads(template.required_labels_json or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str))


def _extras_table_available(session: Session) -> bool:
    return sa_inspect(session.connection()).has_table("metrictemplateextras")


def extras_for(session: Session, template_id: int | None) -> TemplateExtras:
    """Defaults when there is no row — which is what every template starts with."""

    if template_id is None or not _extras_table_available(session):
        return TemplateExtras()
    row = session.exec(
        select(MetricTemplateExtras).where(
            MetricTemplateExtras.template_id == int(template_id)
        )
    ).first()
    if row is None:
        return TemplateExtras()
    return TemplateExtras(
        priority=int(row.priority), display_unit=str(row.display_unit or "")
    )


def extras_for_many(
    session: Session, template_ids: list[int]
) -> dict[int, TemplateExtras]:
    """One lookup for sorting/listing an imported catalogue, never one per row."""

    ids = [int(item) for item in template_ids]
    if not ids or not _extras_table_available(session):
        return {}
    rows = session.exec(
        select(MetricTemplateExtras).where(
            MetricTemplateExtras.template_id.in_(ids)
        )
    ).all()
    return {
        int(row.template_id): TemplateExtras(
            priority=int(row.priority), display_unit=str(row.display_unit or "")
        )
        for row in rows
    }


def set_template_extras(
    session: Session,
    template_id: int,
    *,
    priority: int | None = None,
    display_unit: str | None = None,
) -> TemplateExtras:
    """Upsert one template's ordering/display. Only the given fields change."""

    if not _extras_table_available(session):
        return TemplateExtras()
    row = session.exec(
        select(MetricTemplateExtras).where(
            MetricTemplateExtras.template_id == int(template_id)
        )
    ).first()
    if row is None:
        row = MetricTemplateExtras(template_id=int(template_id))
    if priority is not None:
        row.priority = int(priority)
    if display_unit is not None:
        row.display_unit = str(display_unit)
    row.updated_at = utcnow()
    session.add(row)
    session.flush()
    return TemplateExtras(priority=row.priority, display_unit=row.display_unit)


@dataclass(frozen=True)
class TemplateOrigin:
    """Where an imported template came from, for the list badge and the link."""

    source_id: str
    dashboard_uid: str
    dashboard_title: str
    panel_id: int
    panel_title: str


def _origin_table_available(session: Session) -> bool:
    return sa_inspect(session.connection()).has_table("metrictemplateorigin")


def _scope_table_available(session: Session) -> bool:
    return sa_inspect(session.connection()).has_table("metrictemplatesourcescope")


def template_scopes_for(
    session: Session, template_ids: list[int]
) -> dict[int, dict[str, object]]:
    """Batch Source Scope lookup. Missing side rows deliberately mean ALL."""

    ids = [int(item) for item in template_ids]
    if not ids or not _scope_table_available(session):
        return {}
    rows = session.exec(
        select(MetricTemplateSourceScope).where(
            MetricTemplateSourceScope.template_id.in_(ids)
        )
    ).all()
    return {
        int(row.template_id): source_scope_dict(
            str(row.scope_mode), list(row.source_ids_json or [])
        )
        for row in rows
    }


def template_scope_for(session: Session, template_id: int | None) -> dict[str, object]:
    if template_id is None:
        return source_scope_dict("ALL", [])
    return template_scopes_for(session, [int(template_id)]).get(
        int(template_id), source_scope_dict("ALL", [])
    )


def set_template_scope(
    session: Session, template_id: int, raw_scope: dict | None
) -> dict[str, object]:
    """Validate and replace one scope. ALL is represented by no side row."""

    mode, source_ids = validate_source_scope(session, raw_scope)
    if not _scope_table_available(session):
        if mode == "SELECTED":
            raise ValueError("source scope table is unavailable")
        return source_scope_dict("ALL", [])
    row = session.exec(
        select(MetricTemplateSourceScope).where(
            MetricTemplateSourceScope.template_id == int(template_id)
        )
    ).first()
    if mode == "ALL":
        if row is not None:
            session.delete(row)
            session.flush()
        return source_scope_dict("ALL", [])
    if row is None:
        row = MetricTemplateSourceScope(template_id=int(template_id))
    row.scope_mode = "SELECTED"
    row.source_ids_json = list(source_ids)
    row.updated_at = utcnow()
    session.add(row)
    session.flush()
    return source_scope_dict("SELECTED", source_ids)


def origins_for(session: Session, template_ids: list[int]) -> dict[int, TemplateOrigin]:
    """Batch lookup, so listing N templates stays one query rather than N.

    A template with no row here was written by hand — that absence is what
    distinguishes the two on screen.
    """

    if not template_ids or not _origin_table_available(session):
        return {}
    rows = session.exec(
        select(MetricTemplateOrigin).where(
            MetricTemplateOrigin.template_id.in_(template_ids)
        )
    ).all()
    return {
        int(row.template_id): TemplateOrigin(
            source_id=row.source_id,
            dashboard_uid=row.dashboard_uid,
            dashboard_title=row.dashboard_title,
            panel_id=int(row.panel_id),
            panel_title=row.panel_title,
        )
        for row in rows
    }


def delete_template_side_rows(session: Session, template_id: int) -> None:
    """Remove current side rows belonging to a template being deleted.

    The foreign keys declare `ON DELETE CASCADE`, but that only takes effect
    where SQLite's `foreign_keys` pragma is enabled. Doing it here as well makes
    the current product cleanup independent of how the engine was constructed.
    Historical baseline rows are not read or written here (ADR 0015); the real
    database connection enables foreign-key cascades.
    """
    if _origin_table_available(session):
        for row in session.exec(
            select(MetricTemplateOrigin).where(
                MetricTemplateOrigin.template_id == int(template_id)
            )
        ).all():
            session.delete(row)
    if _scope_table_available(session):
        for row in session.exec(
            select(MetricTemplateSourceScope).where(
                MetricTemplateSourceScope.template_id == int(template_id)
            )
        ).all():
            session.delete(row)
    session.flush()


def seed_builtin_templates(session: Session) -> int:
    """Insert or refresh the shipped catalogue. Safe to run on every startup.

    Runs here rather than in migration 11 because these queries have to keep
    changing as they meet real clusters, and an applied migration's checksum is
    frozen the moment it lands anywhere.

    A row the user has edited is **left alone** (`user_modified`), and `enabled`
    is never touched after the first insert — re-seeding must not switch a curve
    back on that someone turned off. Same trade-off as the F21 one-time adoption:
    what the user changed wins.
    """

    if not _table_available(session):
        return 0

    existing = {
        row.builtin_key: row
        for row in session.exec(
            select(MetricQueryTemplate).where(
                MetricQueryTemplate.builtin_key.is_not(None)
            )
        ).all()
    }

    written = 0
    for builtin in BUILTIN_TEMPLATES:
        row = existing.get(builtin.key)
        if row is None:
            session.add(
                MetricQueryTemplate(
                    name=builtin.name,
                    promql=builtin.promql,
                    required_labels_json=json.dumps(list(builtin.required_labels)),
                    description=builtin.description,
                    builtin_key=builtin.key,
                    user_modified=False,
                    enabled=False,
                )
            )
            written += 1
            continue
        if row.user_modified:
            continue
        changed = (
            row.name != builtin.name
            or row.promql != builtin.promql
            or row.description != builtin.description
            or required_labels(row) != builtin.required_labels
        )
        if changed:
            row.name = builtin.name
            row.promql = builtin.promql
            row.description = builtin.description
            row.required_labels_json = json.dumps(list(builtin.required_labels))
            row.updated_at = utcnow()
            session.add(row)
            written += 1
    session.flush()
    return written


def matches(template: MetricQueryTemplate, labels: Mapping[str, str]) -> bool:
    """A template is a candidate only when the alert carries every label it needs.

    All-or-nothing on purpose: a partially filled template would leave a literal
    `{{pod}}` in the query, and a query that still contains its placeholder either
    errors upstream or — worse — matches nothing and looks like a missing metric.
    """

    for name in required_labels(template):
        value = labels.get(name)
        if value is None or not str(value).strip():
            return False
    return True


def render(template: MetricQueryTemplate, labels: Mapping[str, str]) -> str | None:
    """Fill the placeholders, escaping every value. None when anything is missing.

    Label values are external text: they arrive from the monitored system and
    must not be able to close a matcher and append their own.
    """

    missing = False

    def substitute(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1)
        value = labels.get(name)
        if value is None or not str(value).strip():
            missing = True
            return ""
        return escape_label_value(str(value))

    rendered = _PLACEHOLDER.sub(substitute, template.promql or "")
    if missing or not rendered.strip():
        return None
    return rendered


def placeholder_names(query: str) -> tuple[str, ...]:
    """Names that still need an Alert context; inspecting never rewrites query text."""

    return tuple(dict.fromkeys(match.group(1) for match in _PLACEHOLDER.finditer(query)))


def candidate_templates(
    session: Session,
    labels: Mapping[str, str],
    *,
    source_id: str = "",
    limit: int = MAX_AUXILIARY_CURVES,
) -> CandidateSelection:
    """Enabled templates this alert satisfies, in explicit priority order.

    Ordered by `priority ASC, id ASC` rather than by name (D40): with name order,
    which curves survived the cap depended on what someone called them, and the
    ones that did not survive vanished without a word. The count of what was
    omitted comes back with the selection so the caller can say so.
    """

    if not _table_available(session):
        return CandidateSelection(selected=[], matched=0, omitted=0)
    rows = session.exec(
        select(MetricQueryTemplate).where(MetricQueryTemplate.enabled.is_(True))
    ).all()
    ids = [int(row.id or 0) for row in rows]
    scopes = template_scopes_for(session, ids)
    matching = [
        row
        for row in rows
        if matches(row, labels)
        and scope_contains(
            str(scopes.get(int(row.id or 0), {}).get("mode") or "ALL"),
            scopes.get(int(row.id or 0), {}).get("source_ids") or [],
            source_id,
        )
    ]
    extras = extras_for_many(session, [int(row.id or 0) for row in matching])
    matching.sort(
        key=lambda row: (
            extras.get(int(row.id or 0), TemplateExtras()).priority,
            int(row.id or 0),
        )
    )
    bound = max(0, limit)
    return CandidateSelection(
        selected=matching[:bound],
        matched=len(matching),
        omitted=max(0, len(matching) - bound),
    )
