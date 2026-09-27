"""Pure metric budget, template selection and Grafana parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit

MAX_RANGE_SECONDS = 24 * 3600
MAX_SERIES_PER_QUERY = 20
MAX_QUERIES_PER_ALERT = 6
MAX_PROBES_PER_IMPORT = 40
MAX_PROBE_CONCURRENCY = 4
MAX_PROMQL_LENGTH = 4096
MAX_GENERATOR_URL_LENGTH = 8 * 1024


class QueryBudgetExceeded(ValueError):
    """A code-only refusal raised before a monitoring read is attempted."""


@dataclass(frozen=True, slots=True)
class BackfillWindow:
    start: datetime
    end: datetime
    requested_hours: int
    effective_hours: int
    truncated: bool


def backfill_window(
    now: datetime, *, requested_hours: int = 24, hard_limit_hours: int = 168
) -> BackfillWindow:
    requested = max(0, requested_hours)
    effective = min(requested, max(0, hard_limit_hours))
    return BackfillWindow(
        now - timedelta(hours=effective),
        now,
        requested,
        effective,
        requested > effective,
    )


def reconstruct_alerts(data: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Turn a bounded Thanos ALERTS matrix into stable historical alerts."""

    results = data.get("result")
    if data.get("resultType") != "matrix" or not isinstance(results, list):
        return ()
    reconstructed: list[dict[str, Any]] = []
    for series in results:
        if not isinstance(series, dict) or not isinstance(series.get("metric"), dict):
            continue
        timestamps: list[float] = []
        for sample in series.get("values") or ():
            if not isinstance(sample, list) or len(sample) < 2:
                continue
            try:
                if float(sample[1]) > 0:
                    timestamps.append(float(sample[0]))
            except (TypeError, ValueError):
                continue
        if not timestamps:
            continue
        labels = {
            str(key): str(value)
            for key, value in series["metric"].items()
            if key not in {"__name__", "alertstate"}
        }
        canonical = json.dumps(labels, sort_keys=True, separators=(",", ":"))

        def timestamp(value: float) -> str:
            return (
                datetime.fromtimestamp(value, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        ordered = sorted(timestamps)
        reconstructed.append(
            {
                "fingerprint": "backfill-"
                + hashlib.sha256(canonical.encode()).hexdigest()[:20],
                "labels": labels,
                "annotations": {},
                "startsAt": timestamp(ordered[0]),
                "endsAt": timestamp(ordered[-1]),
                "status": {"state": "historical"},
            }
        )
    return tuple(sorted(reconstructed, key=lambda item: str(item["fingerprint"])))


class MetricReadStatus(str, Enum):
    SUCCESS = "SUCCESS"
    EMPTY_NO_DATA = "EMPTY_NO_DATA"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class QueryWindow:
    start: datetime
    end: datetime
    step_seconds: int

    def validate(self, *, query_count: int = 1) -> None:
        span = (self.end - self.start).total_seconds()
        if span <= 0:
            raise QueryBudgetExceeded("METRIC_WINDOW_INVALID")
        if span > MAX_RANGE_SECONDS:
            raise QueryBudgetExceeded("METRIC_RANGE_TOO_LONG")
        if self.step_seconds < 1:
            raise QueryBudgetExceeded("METRIC_STEP_INVALID")
        if not 1 <= query_count <= MAX_QUERIES_PER_ALERT:
            raise QueryBudgetExceeded("METRIC_TOO_MANY_QUERIES")


@dataclass(frozen=True, slots=True)
class MetricReadResult:
    status: MetricReadStatus
    series: tuple[Mapping[str, Any], ...] = ()
    safe_error_code: str | None = None


def classify_read(data: Mapping[str, Any]) -> MetricReadResult:
    result_type = data.get("resultType")
    raw = data.get("result")
    if result_type not in {"matrix", "vector"} or not isinstance(raw, list):
        raise ValueError("METRIC_RESPONSE_INVALID")
    if len(raw) > MAX_SERIES_PER_QUERY:
        raise QueryBudgetExceeded("METRIC_TOO_MANY_SERIES")
    if not raw:
        return MetricReadResult(MetricReadStatus.EMPTY_NO_DATA)
    if any(not isinstance(item, dict) for item in raw):
        raise ValueError("METRIC_RESPONSE_INVALID")
    return MetricReadResult(MetricReadStatus.SUCCESS, tuple(raw))


@dataclass(frozen=True, slots=True)
class MetricTemplate:
    id: int
    name: str
    promql: str
    priority: int
    enabled: bool
    source_ids: tuple[str, ...]
    origin_kind: str
    required_labels: tuple[str, ...] = ()


def select_templates(
    templates: Sequence[MetricTemplate],
    *,
    source_id: str,
    limit: int = MAX_QUERIES_PER_ALERT - 1,
) -> tuple[MetricTemplate, ...]:
    candidates = (
        item
        for item in templates
        if item.enabled and (not item.source_ids or source_id in item.source_ids)
    )
    return tuple(sorted(candidates, key=lambda item: (item.priority, item.id))[:limit])


@dataclass(frozen=True, slots=True)
class GrafanaCandidate:
    dashboard_uid: str
    dashboard_title: str
    panel_id: int
    panel_title: str
    ref_id: str
    imported_promql: str
    required_variables: tuple[str, ...]
    legend_format: str = ""
    unit: str = ""
    status: str = "READY"
    reason: str = ""


_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}|([A-Za-z_][A-Za-z0-9_]*))")
_SUPPORTED_GRAFANA_PANELS = {"", "graph", "timeseries", "stat", "gauge", "bargauge"}
_TIME_WINDOW_MACROS = ("$__from", "${__from}", "$__to", "${__to}", "$__range", "${__range}")


def _targets(panel: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    raw = panel.get("targets")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict))


def _panels(dashboard: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    found: list[Mapping[str, Any]] = []
    pending = list(dashboard.get("panels") or [])
    while pending:
        item = pending.pop(0)
        if not isinstance(item, dict):
            continue
        found.append(item)
        nested = item.get("panels")
        if isinstance(nested, list):
            pending[0:0] = nested
    return tuple(found)


def _replace_grafana_macros(promql: str) -> str:
    # Import owns one conservative rate interval. Time-bound macros keep their
    # meaning and therefore remain unsupported instead of being guessed.
    return (
        promql.replace("$__rate_interval", "5m")
        .replace("${__rate_interval}", "5m")
        .replace("$__interval", "5m")
        .replace("${__interval}", "5m")
    )


def _grafana_datasource_type(
    panel: Mapping[str, Any], target: Mapping[str, Any]
) -> str:
    datasource = target.get("datasource", panel.get("datasource"))
    if datasource is None:
        return "default"
    if isinstance(datasource, str):
        return "variable" if datasource.startswith("$") else datasource.lower()
    if isinstance(datasource, dict):
        value = str(datasource.get("type") or "default").lower()
        return "variable" if value.startswith("$") else value
    return "unknown"


def extract_grafana_candidates(
    dashboard: Mapping[str, Any], *, dashboard_uid: str
) -> tuple[GrafanaCandidate, ...]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dashboard_uid):
        raise ValueError("GRAFANA_UID_INVALID")
    dashboard_title = str(dashboard.get("title") or dashboard_uid)[:256]
    candidates: list[GrafanaCandidate] = []
    for panel in _panels(dashboard):
        panel_id = panel.get("id")
        if not isinstance(panel_id, int):
            continue
        panel_type = str(panel.get("type") or "").lower()
        for target in _targets(panel):
            if target.get("hide") is True:
                continue
            raw_promql = str(target.get("expr") or "").strip()
            if not raw_promql or len(raw_promql) > MAX_PROMQL_LENGTH:
                continue
            datasource_type = _grafana_datasource_type(panel, target)
            status = "READY"
            reason = ""
            if datasource_type == "variable":
                status = "UNSUPPORTED"
                reason = "数据源由 dashboard 变量决定，无法确认查询会访问哪个存储。"
            elif datasource_type not in {"default", "prometheus"}:
                status = "UNSUPPORTED"
                reason = (
                    f"数据源类型 {datasource_type} 不支持；只允许导入 Prometheus 查询。"
                )
            elif panel_type not in _SUPPORTED_GRAFANA_PANELS:
                status = "UNSUPPORTED"
                reason = f"panel 类型 {panel_type or 'unknown'} 不支持，它没有单一时序读数。"
            window_macro = next(
                (macro for macro in _TIME_WINDOW_MACROS if macro in raw_promql), None
            )
            if status == "READY" and window_macro is not None:
                status = "UNSUPPORTED"
                reason = (
                    f"查询使用 {window_macro}，其 dashboard 时间窗语义不能安全替换为告警窗口。"
                )
            imported = (
                raw_promql
                if status == "UNSUPPORTED"
                else _replace_grafana_macros(raw_promql)
            )
            variables = tuple(
                dict.fromkeys(
                    name
                    for groups in _VARIABLE.findall(imported)
                    for name in groups
                    if name and not name.startswith("__")
                )
            )
            if status == "READY" and variables:
                status = "NEEDS_DECISION"
                reason = "查询包含 dashboard 变量，导入前需要绑定告警标签或固定值。"
            candidates.append(
                GrafanaCandidate(
                    dashboard_uid=dashboard_uid,
                    dashboard_title=dashboard_title,
                    panel_id=panel_id,
                    panel_title=str(panel.get("title") or f"Panel {panel_id}")[:256],
                    ref_id=str(target.get("refId") or "A")[:32],
                    imported_promql=imported,
                    required_variables=variables,
                    legend_format=str(target.get("legendFormat") or "")[:256],
                    unit=str(
                        ((panel.get("fieldConfig") or {}).get("defaults") or {}).get("unit")
                        or ""
                    )[:64]
                    if isinstance(panel.get("fieldConfig"), dict)
                    else "",
                    status=status,
                    reason=reason,
                )
            )
    return tuple(candidates)


def grafana_deep_link(
    base_url: str,
    *,
    dashboard_uid: str,
    panel_id: int,
    start_ms: int,
    end_ms: int,
) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dashboard_uid):
        raise ValueError("GRAFANA_UID_INVALID")
    query = urlencode(
        {
            "viewPanel": panel_id,
            "from": start_ms,
            "to": end_ms,
        }
    )
    return f"{base_url.rstrip('/')}/d/{dashboard_uid}?{query}"


_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_NUMBER = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|[Ii][Nn][Ff]|[Nn][Aa][Nn])$"
)
_COMPARISON_OPERATORS = ("==", "!=", "<=", ">=", "<", ">")


def placeholder_names(query: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(match.group(1) for match in _TEMPLATE_PLACEHOLDER.finditer(query))
    )


def render_metric_template(
    query: str,
    labels: Mapping[str, str],
    *,
    required_labels: Sequence[str] = (),
) -> str | None:
    """Render one template without allowing an alert label to escape a matcher."""

    if not query.strip() or len(query) > MAX_PROMQL_LENGTH:
        return None
    for name in required_labels:
        value = labels.get(name)
        if value is None or not str(value).strip():
            return None
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        value = labels.get(match.group(1))
        if value is None or not str(value).strip():
            missing = True
            return ""
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace('"', '\\"')
        )

    rendered = _TEMPLATE_PLACEHOLDER.sub(replace, query)
    return None if missing or not rendered.strip() else rendered


def expr_from_generator_url(url: str | None) -> str | None:
    """Extract Prometheus' first graph expression; never request the URL."""

    if not url or len(url) > MAX_GENERATOR_URL_LENGTH:
        return None
    try:
        values = parse_qs(urlsplit(url).query, keep_blank_values=False)
    except ValueError:
        return None
    for candidate in values.get("g0.expr") or ():
        expression = candidate.strip()
        if expression and len(expression) <= MAX_PROMQL_LENGTH:
            return expression
    return None


def strip_numeric_comparison(expression: str) -> str | None:
    """Conservatively strip one top-level numeric threshold comparison."""

    text = expression.strip()
    if not text or len(text) > MAX_PROMQL_LENGTH:
        return None
    depth = {"(": 0, "{": 0, "[": 0}
    closing = {")": "(", "}": "{", "]": "["}
    comparisons: list[tuple[int, str]] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote != "`":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'", "`"}:
            quote = char
            index += 1
            continue
        if char in depth:
            depth[char] += 1
            index += 1
            continue
        if char in closing:
            key = closing[char]
            depth[key] -= 1
            if depth[key] < 0:
                return None
            index += 1
            continue
        if not any(depth.values()):
            operator = next(
                (item for item in _COMPARISON_OPERATORS if text.startswith(item, index)),
                None,
            )
            if operator is not None:
                comparisons.append((index, operator))
                index += len(operator)
                continue
        index += 1
    if quote is not None or any(depth.values()) or len(comparisons) != 1:
        return None
    position, operator = comparisons[0]
    left = text[:position].strip()
    right = text[position + len(operator) :].strip()
    if not left or not right:
        return None
    left_number = _NUMBER.fullmatch(left) is not None
    right_number = _NUMBER.fullmatch(right) is not None
    if left_number == right_number:
        return None
    return right if left_number else left
