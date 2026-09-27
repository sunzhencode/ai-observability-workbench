"""Turn a Grafana dashboard definition into reviewable import candidates.

Deterministic and offline: `parse_dashboard` and `diff_candidates` take
already-fetched data and return plain values, which is what lets the whole
branch matrix be tested against one JSON file. Only `probe_candidates` touches
the network, and it takes a client it does not construct.

**The whole point of this module is that it produces a proposal, not a result.**
Nothing here writes to the database; the user ticks candidates and only then
does `confirm_import` store anything (CAP-13.2, the same rule aggregation rule
previews follow).

Two decisions shape everything below:

*Macros and variables are resolved once, at import, and never again.* The
resolved text is shown to the user, and what gets stored on confirmation is a
literal. From that moment the template is indistinguishable from a hand-written
one, and the query path never rewrites it (CAP-12.5). Putting substitution in
the query path instead would mean an expression its author wrote in full gets
quietly reprocessed — the defect the auto-`rate()` rule is already fenced
against.

*The datasource is checked before the panel type.* Loki, SQL and Elasticsearch
targets carry an `expr` too, so without that ordering a Loki panel parses fine
and fails at probe time as a "syntax error" — an answer that sends the reader
somewhere with nothing to find (design §14 R-1).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence

from sqlmodel import select as sql_select

from app.models import utcnow
from app.registry_models import (
    EventSource,
    MetricQueryTemplate,
    MetricTemplateOrigin,
)
from app.services.metric_budget import (
    BudgetExceeded,
    MAX_PROBE_CONCURRENCY,
    MAX_PROBES_PER_IMPORT,
    MAX_SERIES_PER_QUERY,
    PROBE_TIMEOUT_SECONDS,
    assert_series_count_within_budget,
)
from app.services.metric_templates import (
    DEFAULT_TEMPLATE_PRIORITY,
    extras_for,
    set_template_extras,
    set_template_scope,
)
from app.services.promql import (
    QueryScopeUnsafe,
    assert_query_scope_safe,
    extract_metric_names,
)
from app.sources.thanos import safe_error_code

# --- what the panel walk accepts -------------------------------------------

#: Types with a single time-series reading. `table`, `text` and `heatmap` are
#: left out because they either have no such reading or cost more to interpret
#: than the result is worth; `row` is a container and yields nothing itself.
SUPPORTED_PANEL_TYPES = frozenset(
    {"timeseries", "graph", "stat", "gauge", "bargauge"}
)

#: Variables whose name matches an alert label are suggested for binding.
#: `{{instance}}` is already how the built-in templates are written, so this is
#: the native form rather than a new concept.
LABEL_SHAPED_VARIABLES = frozenset(
    {"cluster", "instance", "namespace", "pod", "job"}
)

#: Grafana's "everything" sentinel. Never a value any series carries.
ALL_SENTINEL = "$__all"

# --- macro handling ---------------------------------------------------------

#: Longest name first: `$__interval_ms` must not be rewritten as `5m` plus a
#: stray `_ms`, and `$__range_s` must not become `1h_s`.
MACRO_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("__rate_interval", "5m"),
    ("__interval_ms", "300000"),
    ("__interval", "5m"),
    ("__range_ms", "3600000"),
    ("__range_s", "3600"),
    ("__range", "1h"),
)

#: Macros standing for "the panel's current time window". They are refused, not
#: substituted: this system's window comes from when the alert fired, and a
#: concrete timestamp here produces a curve that renders perfectly and answers a
#: different question (CAP-13.3).
TIME_WINDOW_MACROS: tuple[str, ...] = (
    "__from",
    "__to",
    "__timeFilter",
    "timeFilter",
)

_VARIABLE_PATTERN = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


class CandidateStatus(str, Enum):
    """What the user is being told about one candidate.

    `UNVERIFIED` is a fourth outcome and not a shade of the other three: it
    means "this was never checked against a store", which must never be
    presented as working. Collapsing it into `READY` is precisely the lie the
    live probe exists to prevent (design §5.4).
    """

    READY = "READY"
    NEEDS_DECISION = "NEEDS_DECISION"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"


class DiffKind(str, Enum):
    NEW = "NEW"
    UNCHANGED = "UNCHANGED"
    UPSTREAM_CHANGED = "UPSTREAM_CHANGED"
    CONFLICT = "CONFLICT"
    GONE = "GONE"


@dataclass(frozen=True)
class Substitution:
    """One macro that was replaced, so the change can be shown, not just made."""

    macro: str
    replacement: str


@dataclass(frozen=True)
class PendingVariable:
    """A dashboard variable the user has to decide about: bind, pin, or skip.

    The code suggests and never decides. A bound variable narrows the query to
    whatever the alert carries; a pinned one freezes a value forever. Both are
    reasonable and neither is inferable from the JSON.
    """

    name: str
    suggestion: str  # BIND_LABEL / PIN_VALUE
    sample_value: str = ""
    multi: bool = False


@dataclass(frozen=True)
class Candidate:
    """One panel target, resolved as far as code can take it."""

    panel_id: int
    panel_title: str
    ref_id: str
    raw_promql: str
    resolved_promql: str
    status: CandidateStatus
    suggested_name: str
    order: int
    reason: str = ""
    substitutions: tuple[Substitution, ...] = ()
    pending_variables: tuple[PendingVariable, ...] = ()
    display_unit: str = ""
    #: The first sample the live probe saw, retained as transparent evidence of
    #: what the import validation actually observed.
    probe_value: float | None = None
    #: How thoroughly it was checked, in plain words. "Verified with the sample
    #: value 10.0.0.5" and "only the metric name was checked" are different
    #: promises and must not read the same.
    probe_note: str = ""


@dataclass(frozen=True)
class DiffEntry:
    """A candidate placed against what was imported from this dashboard before.

    `candidate` is None only for `GONE`, where the target no longer exists
    upstream and all that remains is the stored origin.
    """

    kind: DiffKind
    candidate: Candidate | None
    template_id: int | None = None
    imported_promql: str = ""
    current_promql: str = ""
    panel_id: int = 0
    panel_title: str = ""
    ref_id: str = ""


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _walk_panels(panels: Iterable[Any]) -> list[dict[str, Any]]:
    """Flatten one level of row nesting, which is all Grafana's schema has.

    A collapsed row carries its children inline. Skipping that silently hides
    whole sections of a dashboard, and nothing in the result would say so.
    """

    flattened: list[dict[str, Any]] = []
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        if panel.get("type") == "row":
            flattened.extend(_walk_panels(panel.get("panels") or []))
            continue
        flattened.append(panel)
    return flattened


def _datasource_kind(raw: Any) -> str:
    """Classify a datasource reference into one word.

    Returns `default` when nothing is declared: older dashboards omit the field
    and mean "the default datasource". Rejecting those would reject most of what
    a long-lived Grafana contains.
    """

    if raw is None:
        return "default"
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return "default"
        if value == "-- Mixed --":
            return "mixed"
        if value.startswith("$"):
            return "variable"
        # A legacy name string ("Prometheus") does not carry a type. It is
        # accepted rather than guessed at: the live probe is the honest place
        # for that answer, and it gives one.
        return "default"
    if isinstance(raw, dict):
        uid = str(raw.get("uid") or "").strip()
        kind = str(raw.get("type") or "").strip().lower()
        if uid == "-- Mixed --" or kind == "mixed":
            return "mixed"
        if uid.startswith("$") or kind.startswith("$"):
            return "variable"
        if kind in ("", "datasource"):
            return "default"
        return kind
    return "default"


def _effective_datasource(panel: dict[str, Any], target: dict[str, Any]) -> str:
    target_kind = _datasource_kind(target.get("datasource"))
    if target_kind != "default":
        return target_kind
    panel_kind = _datasource_kind(panel.get("datasource"))
    if panel_kind == "mixed":
        # A mixed panel whose target names nothing falls back to the default.
        return "default"
    return panel_kind


def _apply_macros(expression: str) -> tuple[str, tuple[Substitution, ...]]:
    resolved = expression
    substitutions: list[Substitution] = []
    for name, replacement in MACRO_REPLACEMENTS:
        pattern = re.compile(r"\$(?:\{" + re.escape(name) + r"\}|" + re.escape(name) + r"\b)")
        if pattern.search(resolved):
            resolved = pattern.sub(replacement, resolved)
            substitutions.append(Substitution(macro=f"${name}", replacement=replacement))
    return resolved, tuple(substitutions)


def _time_window_macro(expression: str) -> str | None:
    for name in TIME_WINDOW_MACROS:
        pattern = re.compile(r"\$(?:\{" + re.escape(name) + r"\}|" + re.escape(name) + r"\b)")
        if pattern.search(expression):
            return f"${name}"
    return None


def _variable_definitions(dashboard: dict[str, Any]) -> dict[str, dict[str, Any]]:
    templating = dashboard.get("templating")
    entries = templating.get("list") if isinstance(templating, dict) else None
    definitions: dict[str, dict[str, Any]] = {}
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("name"):
            definitions[str(entry["name"])] = entry
    return definitions


def _sample_value(definition: dict[str, Any] | None) -> str:
    if not definition:
        return ""
    current = definition.get("current")
    if not isinstance(current, dict):
        return ""
    value = current.get("value")
    if isinstance(value, list):
        for item in value:
            if str(item) != ALL_SENTINEL:
                return str(item)
        return ""
    if value is None or str(value) == ALL_SENTINEL:
        return ""
    return str(value)


def _pending_variables(
    expression: str, definitions: dict[str, dict[str, Any]]
) -> tuple[PendingVariable, ...]:
    seen: list[PendingVariable] = []
    names: list[str] = []
    for match in _VARIABLE_PATTERN.finditer(expression):
        name = match.group("braced") or match.group("bare")
        if name.startswith("__") or name in names:
            continue
        names.append(name)
        definition = definitions.get(name)
        multi = bool(definition.get("multi")) if definition else False
        # A multi-value variable expands upstream into a regex alternation. One
        # alert carries one value per label, so binding it would quietly mean
        # something narrower than the panel ever showed.
        suggestion = (
            "BIND_LABEL"
            if name in LABEL_SHAPED_VARIABLES and not multi
            else "PIN_VALUE"
        )
        seen.append(
            PendingVariable(
                name=name,
                suggestion=suggestion,
                sample_value=_sample_value(definition),
                multi=multi,
            )
        )
    return tuple(seen)


def _display_unit(panel: dict[str, Any]) -> str:
    defaults = (panel.get("fieldConfig") or {}).get("defaults") or {}
    return str(defaults.get("unit") or "")


def parse_dashboard(dashboard: dict[str, Any]) -> list[Candidate]:
    """Read a dashboard definition into candidates. No network, no session.

    One candidate per target rather than per panel: the A/B/C of a panel are
    usually three different metrics, and a template is matched by labels and
    enabled on its own, so gluing them together would both lose two of them and
    make the survivor unmanageable.
    """

    dashboard_title = str(dashboard.get("title") or "")
    definitions = _variable_definitions(dashboard)
    candidates: list[Candidate] = []
    order = 0

    for panel in _walk_panels(dashboard.get("panels") or []):
        panel_id = int(panel.get("id") or 0)
        panel_title = str(panel.get("title") or "")
        panel_type = str(panel.get("type") or "").lower()
        unit = _display_unit(panel)
        in_panel: list[Candidate] = []

        for target in panel.get("targets") or []:
            if not isinstance(target, dict):
                continue
            expression = str(target.get("expr") or "").strip()
            if not expression:
                continue
            ref_id = str(target.get("refId") or "")
            legend = str(target.get("legendFormat") or "").strip()

            base = dict(
                panel_id=panel_id,
                panel_title=panel_title,
                ref_id=ref_id,
                raw_promql=expression,
                order=order,
                display_unit=unit,
            )
            order += 1

            # --- datasource first, then panel type (design §14 R-1) ---
            datasource = _effective_datasource(panel, target)
            if datasource == "variable":
                in_panel.append(
                    Candidate(
                        **base,
                        resolved_promql=expression,
                        status=CandidateStatus.UNSUPPORTED,
                        suggested_name="",
                        reason=(
                            "这个 panel 的数据源是一个变量，导入时无法确定它指向哪个"
                            "存储，因此不能判断这条查询会跑在哪里。"
                        ),
                    )
                )
                continue
            if datasource not in ("prometheus", "default"):
                in_panel.append(
                    Candidate(
                        **base,
                        resolved_promql=expression,
                        status=CandidateStatus.UNSUPPORTED,
                        suggested_name="",
                        reason=(
                            f"数据源类型 {datasource} 不支持：本产品只导入 "
                            "Prometheus 协议的查询，其它类型用的是另一套查询语言。"
                        ),
                    )
                )
                continue
            if panel_type not in SUPPORTED_PANEL_TYPES:
                in_panel.append(
                    Candidate(
                        **base,
                        resolved_promql=expression,
                        status=CandidateStatus.UNSUPPORTED,
                        suggested_name="",
                        reason=(
                            f"panel 类型 {panel_type} 不支持，它没有单一的时序读数。"
                        ),
                    )
                )
                continue

            window_macro = _time_window_macro(expression)
            if window_macro is not None:
                in_panel.append(
                    Candidate(
                        **base,
                        resolved_promql=expression,
                        status=CandidateStatus.UNSUPPORTED,
                        suggested_name="",
                        reason=(
                            f"这条查询用了 {window_macro}：它表示面板当前的时间窗，"
                            "与本系统按告警时刻决定的窗口语义不同。硬替换会画出一条"
                            "看起来正常、含义却错的曲线。"
                        ),
                    )
                )
                continue

            resolved, substitutions = _apply_macros(expression)
            pending = _pending_variables(resolved, definitions)
            in_panel.append(
                Candidate(
                    **base,
                    resolved_promql=resolved,
                    substitutions=substitutions,
                    pending_variables=pending,
                    status=(
                        CandidateStatus.NEEDS_DECISION
                        if pending
                        else CandidateStatus.READY
                    ),
                    suggested_name=_suggested_name(
                        dashboard_title, panel_title, legend, ref_id, disambiguate=False
                    ),
                )
            )

        # Disambiguation is a property of the panel, not of one target, so it is
        # decided once all its targets are known.
        if len(in_panel) > 1:
            in_panel = [
                replace(
                    item,
                    suggested_name=_suggested_name(
                        dashboard_title,
                        item.panel_title,
                        _legend_for(panel, item.ref_id),
                        item.ref_id,
                        disambiguate=True,
                    ),
                )
                if item.status is not CandidateStatus.UNSUPPORTED
                else item
                for item in in_panel
            ]
        candidates.extend(in_panel)

    return candidates


def _legend_for(panel: dict[str, Any], ref_id: str) -> str:
    for target in panel.get("targets") or []:
        if isinstance(target, dict) and str(target.get("refId") or "") == ref_id:
            return str(target.get("legendFormat") or "").strip()
    return ""


def _suggested_name(
    dashboard_title: str,
    panel_title: str,
    legend: str,
    ref_id: str,
    *,
    disambiguate: bool,
) -> str:
    """`{dashboard} · {panel}`, plus a target suffix only when one is needed.

    The dashboard prefix is not decoration: panel titles repeat heavily across
    dashboards ("CPU", "QPS", "Memory"), and without it the template list stops
    being usable after the second import.
    """

    parts = [part for part in (dashboard_title.strip(), panel_title.strip()) if part]
    name = " · ".join(parts) if parts else "导入的指标模板"
    if disambiguate:
        suffix = legend or ref_id
        if suffix:
            name = f"{name} · {suffix}"
    return name


# ---------------------------------------------------------------------------
# re-import diff
# ---------------------------------------------------------------------------


def diff_candidates(
    candidates: Sequence[Candidate],
    existing_origins: Sequence[Any],
    templates: Sequence[Any],
) -> list[DiffEntry]:
    """Place freshly parsed candidates against what was imported before.

    A first import is the case where nothing aligns and everything is `NEW` —
    the same function, not a separate path, so the two can never drift.

    Historical baseline rows are outside the current product and are not read
    by this diff (ADR 0015).
    """

    by_id = {int(getattr(item, "id", 0) or 0): item for item in templates}
    origins = {
        (int(getattr(origin, "panel_id", 0) or 0), str(getattr(origin, "ref_id", "") or "")): origin
        for origin in existing_origins
    }
    entries: list[DiffEntry] = []
    matched: set[tuple[int, str]] = set()

    for candidate in candidates:
        key = (candidate.panel_id, candidate.ref_id)
        origin = origins.get(key)
        if origin is None:
            entries.append(DiffEntry(kind=DiffKind.NEW, candidate=candidate))
            continue
        matched.add(key)
        template_id = int(getattr(origin, "template_id", 0) or 0)
        template = by_id.get(template_id)
        imported = str(getattr(origin, "imported_promql", "") or "")
        current = str(getattr(template, "promql", "") or "") if template else imported
        if candidate.resolved_promql == imported:
            # Not listed: forty unchanged rows would bury the three that matter.
            continue
        user_modified = bool(getattr(template, "user_modified", False))
        entries.append(
            DiffEntry(
                kind=DiffKind.CONFLICT if user_modified else DiffKind.UPSTREAM_CHANGED,
                candidate=candidate,
                template_id=template_id or None,
                imported_promql=imported,
                current_promql=current,
            )
        )

    for key, origin in origins.items():
        if key in matched:
            continue
        template_id = int(getattr(origin, "template_id", 0) or 0)
        template = by_id.get(template_id)
        entries.append(
            DiffEntry(
                kind=DiffKind.GONE,
                candidate=None,
                template_id=template_id or None,
                imported_promql=str(getattr(origin, "imported_promql", "") or ""),
                current_promql=str(getattr(template, "promql", "") or "")
                if template
                else "",
                panel_id=int(getattr(origin, "panel_id", 0) or 0),
                panel_title=str(getattr(origin, "panel_title", "") or ""),
                ref_id=str(getattr(origin, "ref_id", "") or ""),
            )
        )
    return entries


# ---------------------------------------------------------------------------
# live verification
# ---------------------------------------------------------------------------


def _sample_substituted(candidate: Candidate) -> str | None:
    """Fill pending variables with the dashboard's own current values.

    A candidate still holding `$cluster` is not valid PromQL, so it cannot be
    checked as-is. The dashboard already carries a value the author was looking
    at, which makes a fair smoke test — and the result is labelled as such,
    because "verified with a sample value" promises less than "verified".
    """

    expression = candidate.resolved_promql
    for variable in candidate.pending_variables:
        if not variable.sample_value:
            return None
        pattern = re.compile(
            r"\$(?:\{"
            + re.escape(variable.name)
            + r"(?::[^}]*)?\}|"
            + re.escape(variable.name)
            + r"\b)"
        )
        expression = pattern.sub(variable.sample_value, expression)
    return expression


def _metric_name_probe(candidate: Candidate) -> str | None:
    """Last resort: ask whether the metric name exists at all.

    Strictly less than checking the query, which is why the note says so. An
    unqualified "verified" here would be the quiet overstatement this whole step
    exists to remove.
    """

    names = extract_metric_names(candidate.resolved_promql)
    return names[0] if names else None


def _first_sample_value(payload: dict[str, Any]) -> float | None:
    """The first number the probe saw, whatever shape the answer came in.

    Vectors are the normal case; a `scalar` answer is `[timestamp, "1"]` rather
    than a list of series, and reading only the vector shape would report a
    perfectly good scalar query as "runs but has no data".
    """

    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if payload.get("resultType") == "scalar":
        return _as_float(result)
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    if not isinstance(first, dict):
        return None
    return _as_float(first.get("value"))


def _instant_series_count(payload: dict[str, Any]) -> int:
    """Count a complete instant-query result before anything is displayed.

    `limit` is only an upstream hint and older stores may ignore it, so the
    response itself remains the enforcement boundary. Scalars and strings are
    one result; vectors and matrices contain one item per series.
    """

    result_type = payload.get("resultType")
    result = payload.get("result")
    if result_type in {"scalar", "string"}:
        if not isinstance(result, (list, tuple)):
            raise ValueError("instant query returned an invalid scalar")
        return 1
    if result_type in {"vector", "matrix"}:
        if not isinstance(result, list):
            raise ValueError("instant query returned an invalid series list")
        return len(result)
    raise ValueError("instant query returned an unsupported result type")


def _as_float(value: Any) -> float | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return None
    return None


async def probe_candidates(
    candidates: Sequence[Candidate],
    thanos_client: Any | None,
    *,
    timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> list[Candidate]:
    """Check each candidate against the store it will actually run against.

    Necessary rather than thorough: reading the JSON and guessing which queries
    work gets it wrong, which the built-in catalogue demonstrated once already.

    Three properties are not negotiable. It uses the **instant** endpoint — the
    question is "does this run and return anything", and a range query costs an
    order of magnitude more for the same answer. It stops at
    `MAX_PROBES_PER_IMPORT`, because one click here is a burst rather than a
    single request. And when there is nothing to check against, every candidate
    comes back **unverified** and never "ready": an unchecked query presented as
    working is the precise failure this step exists to prevent.
    """

    timeout = float(
        PROBE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    at = now or datetime.now(timezone.utc)
    results: list[Candidate] = list(candidates)
    checkable = [
        index
        for index, candidate in enumerate(results)
        if candidate.status
        in (CandidateStatus.READY, CandidateStatus.NEEDS_DECISION)
    ]

    configured = bool(thanos_client) and bool(
        getattr(thanos_client, "configured", False)
    )
    if not configured:
        for index in checkable:
            results[index] = replace(
                results[index],
                status=CandidateStatus.UNVERIFIED,
                probe_note=(
                    "该来源未配置历史地址（Thanos），无法验证这条查询能不能跑。"
                ),
            )
        return results

    within_budget = checkable[:MAX_PROBES_PER_IMPORT]
    for index in checkable[MAX_PROBES_PER_IMPORT:]:
        results[index] = replace(
            results[index],
            status=CandidateStatus.UNVERIFIED,
            probe_note=(
                f"超出单次导入实查预算（{MAX_PROBES_PER_IMPORT} 条），这次没有验证。"
                "查询本身没有问题，可以照常勾选。"
            ),
        )

    semaphore = asyncio.Semaphore(MAX_PROBE_CONCURRENCY)

    async def run(index: int) -> None:
        candidate = results[index]
        query = _sample_substituted(candidate)
        note_prefix = ""
        if query is None:
            metric = _metric_name_probe(candidate)
            if metric is None:
                results[index] = replace(
                    candidate,
                    status=CandidateStatus.UNVERIFIED,
                    probe_note="这条查询里没有可用的样例值，本次未做实查。",
                )
                return
            query = metric
            note_prefix = "只验证了指标名是否存在，没有验证整条查询。"
        elif candidate.pending_variables:
            values = "、".join(
                item.sample_value for item in candidate.pending_variables
            )
            note_prefix = f"用样例值 {values} 验过。"

        try:
            assert_query_scope_safe(query)
        except QueryScopeUnsafe:
            results[index] = replace(
                candidate,
                status=CandidateStatus.UNSUPPORTED,
                reason="查询超出共享的范围预算，未向 Thanos 发出请求。",
            )
            return

        async with semaphore:
            try:
                payload = await asyncio.wait_for(
                    thanos_client.query_instant(
                        query, at, limit=MAX_SERIES_PER_QUERY + 1
                    ),
                    timeout=timeout,
                )
                assert_series_count_within_budget(_instant_series_count(payload))
            except asyncio.TimeoutError:
                results[index] = replace(
                    candidate,
                    status=CandidateStatus.UNSUPPORTED,
                    reason=f"实查超时（{timeout:g} 秒），未能确认这条查询能否运行。",
                )
                return
            except BudgetExceeded:
                results[index] = replace(
                    candidate,
                    status=CandidateStatus.UNSUPPORTED,
                    reason=(
                        f"实查返回的序列超过预算（最多 {MAX_SERIES_PER_QUERY} 条），"
                        "没有截断或导入这条候选。"
                    ),
                )
                return
            except Exception as exc:  # noqa: BLE001 - classified, never re-raised
                # One bad candidate must not take the batch down: the user is
                # looking at forty of them and needs the other thirty-nine.
                results[index] = replace(
                    candidate,
                    status=CandidateStatus.UNSUPPORTED,
                    reason=f"实查失败（{safe_error_code(exc)}）。",
                )
                return

        value = _first_sample_value(payload if isinstance(payload, dict) else {})
        if value is None:
            note = "查询能跑，当前窗口没有数据。"
        else:
            note = "查询能跑，有数据。"
        results[index] = replace(
            candidate,
            # A candidate whose variables are still undecided stays undecided
            # however well the smoke test went: the user has yet to choose bind
            # or pin, and promoting it here would let it be ticked with a
            # literal `$cluster` still in the query.
            status=candidate.status,
            probe_value=value,
            probe_note=f"{note_prefix}{note}".strip(),
        )

    await asyncio.gather(*(run(index) for index in within_budget))
    return results


# ---------------------------------------------------------------------------
# writing the chosen candidates
# ---------------------------------------------------------------------------

MAX_PROMQL_LENGTH = 4096


@dataclass(frozen=True)
class ConfirmOrigin:
    dashboard_uid: str
    dashboard_title: str
    panel_id: int
    panel_title: str
    ref_id: str = ""


@dataclass(frozen=True)
class ConfirmItem:
    """One ticked candidate, as the browser finally submitted it.

    `final_promql` is a literal the user has read, not something re-derived
    here. Re-fetching the dashboard at this point would look more trustworthy
    and be less so: it opens a window in which the definition changes between
    preview and confirmation, and what gets stored is then not what was
    approved (review Q4). The trust level is the same as a hand-written
    template, which CAP-12.5 already treats as written whole by its author.
    """

    final_promql: str
    name: str
    origin: ConfirmOrigin
    #: Grafana's own text for this target, after macro substitution but **before
    #: the user's variable bindings** — i.e. the candidate's `resolved_promql`.
    #:
    #: Recorded separately from `final_promql` because the two answer different
    #: questions, and the re-import diff asks this one: "did Grafana change?".
    #: Comparing Grafana's current text against a stored literal that has had
    #: `$instance` rewritten to `{{instance}}` reports every bound candidate as
    #: upstream-changed forever, and reports a *conflict* for any the user has
    #: also edited — a conflict nobody created, on every re-import.
    #:
    #: Empty falls back to `final_promql`, which is correct for the many
    #: candidates that have no variables at all.
    imported_promql: str = ""
    required_labels: tuple[str, ...] = ()
    enabled: bool = False
    display_unit: str = ""
    order: int = 0
    #: Set when accepting an upstream change to an already-imported template.
    template_id: int | None = None


@dataclass
class ConfirmResult:
    created_template_ids: list[int] = field(default_factory=list)
    updated_template_ids: list[int] = field(default_factory=list)


def _validate(item: ConfirmItem) -> str:
    query = str(item.final_promql or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    if len(query) > MAX_PROMQL_LENGTH:
        raise ValueError("query is too long")
    try:
        # The same gate a hand-written template passes when saved. Import is
        # another door onto the same table, and a door without the check is how
        # a guard quietly stops meaning anything.
        assert_query_scope_safe(query)
    except QueryScopeUnsafe as exc:
        raise ValueError("query is outside the scope guard") from exc
    if not str(item.name or "").strip():
        raise ValueError("name must not be empty")
    if not str(item.origin.dashboard_uid or "").strip():
        raise ValueError("dashboard identity is incomplete")
    return query


def _imported_text(item: ConfirmItem, query: str) -> str:
    """What Grafana said, which is what the next re-import compares against."""

    return str(item.imported_promql or "").strip() or query


def _unique_name(taken: set[str], name: str) -> str:
    """Suffix against the whole catalogue, not just this batch.

    Panel titles repeat across dashboards, so the collision is usually with
    something imported weeks ago rather than with a sibling in the same run.
    """

    if name not in taken:
        taken.add(name)
        return name
    index = 2
    while f"{name} ({index})" in taken:
        index += 1
    chosen = f"{name} ({index})"
    taken.add(chosen)
    return chosen


def confirm_import(
    session: Any, source_id: str, items: Sequence[ConfirmItem]
) -> ConfirmResult:
    """Store the ticked candidates. Only these, and nothing else.

    Writes flush but do not commit: the API boundary owns the transaction, so a
    partly-written import cannot survive a failure halfway through (the
    unit-of-work rule this repository has followed since F17).

    Historical `MetricTemplateBaseline` rows are deliberately not read or
    written. ADR 0015 retired that product surface while preserving v14.
    """

    result = ConfirmResult()
    if not items:
        return result

    if session.get(EventSource, source_id) is None:
        raise ValueError("unknown event source")

    prepared = [(item, _validate(item)) for item in items]

    existing_rows = session.exec(sql_select(MetricQueryTemplate)).all()
    taken_names = {str(row.name) for row in existing_rows}
    base_priority = max(
        [extras_for(session, row.id).priority for row in existing_rows]
        + [DEFAULT_TEMPLATE_PRIORITY]
    )

    for offset, (item, query) in enumerate(
        sorted(prepared, key=lambda pair: pair[0].order)
    ):
        if item.template_id is not None:
            template = session.get(MetricQueryTemplate, item.template_id)
            if template is None:
                raise ValueError("template to update does not exist")
            origin = session.exec(
                sql_select(MetricTemplateOrigin).where(
                    MetricTemplateOrigin.template_id == int(item.template_id)
                )
            ).first()
            if origin is None:
                raise ValueError("only an imported template can be updated here")
            identity_matches = (
                str(origin.source_id) == str(source_id)
                and str(origin.dashboard_uid) == str(item.origin.dashboard_uid)
                and int(origin.panel_id) == int(item.origin.panel_id)
                and str(origin.ref_id or "") == str(item.origin.ref_id or "")
            )
            if not identity_matches:
                raise ValueError("template does not match this imported panel target")
            template.promql = query
            # Deliberately not setting `user_modified`: accepting Grafana's
            # version is the opposite of editing it by hand, and the flag would
            # make the next re-import report a conflict nobody created.
            template.updated_at = utcnow()
            session.add(template)
            origin.dashboard_title = item.origin.dashboard_title
            origin.panel_title = item.origin.panel_title
            origin.imported_promql = _imported_text(item, query)
            origin.imported_at = utcnow()
            session.add(origin)
            session.flush()
            result.updated_template_ids.append(int(item.template_id))
            continue

        template = MetricQueryTemplate(
            name=_unique_name(taken_names, str(item.name).strip()),
            promql=query,
            required_labels_json=json.dumps(
                [label for label in item.required_labels if str(label).strip()]
            ),
            description="",
            builtin_key=None,
            user_modified=False,
            # Imported templates arrive switched off, like the shipped ones: a
            # query nobody has watched draw anything is worse switched on,
            # because an empty panel reads as a broken feature rather than as a
            # metric that does not apply here.
            enabled=bool(item.enabled),
        )
        session.add(template)
        session.flush()
        template_id = int(template.id or 0)

        session.add(
            MetricTemplateOrigin(
                template_id=template_id,
                source_id=source_id,
                dashboard_uid=item.origin.dashboard_uid,
                dashboard_title=item.origin.dashboard_title,
                panel_id=int(item.origin.panel_id),
                panel_title=item.origin.panel_title,
                ref_id=str(item.origin.ref_id or ""),
                imported_promql=_imported_text(item, query),
            )
        )
        set_template_extras(
            session,
            template_id,
            priority=base_priority + 10 + offset,
            display_unit=item.display_unit,
        )
        # A dashboard belongs to the Event Source whose Grafana connection read
        # it.  Scope is set only on first creation; re-import must not overwrite a
        # later user decision to widen or move the template.
        set_template_scope(
            session,
            template_id,
            {"mode": "SELECTED", "source_ids": [source_id]},
        )
        session.flush()
        result.created_template_ids.append(template_id)

    return result
