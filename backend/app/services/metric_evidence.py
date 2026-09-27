"""F27 evidence assembly: deterministic, offline, session-and-data only.

Network I/O belongs to `sources/thanos.py`; this module receives what was already
fetched. That split is what lets every tier decision, every degradation and every
failure classification be reproduced from fixtures.

The shape is a small set of pure steps that `api/metrics.py` orchestrates:

    plan_primary(rule_expr, generator_url)      -> PrimaryPlan | failure
    classify_metric_type(metric, metadata)      -> MetricType
    finalize_primary(plan, labels, metric_type) -> query string
    curve_from_matrix(...)                      -> Curve
    build_bundle(curves, failures)              -> EvidenceBundle
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.services import promql
from app.services.metric_budget import (
    MAX_SERIES_PER_QUERY,
    QueryWindow,
    assert_series_count_within_budget,
)

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


class ExprOrigin(str, Enum):
    """Where the expression behind a curve came from.

    Surfaced to the reader on purpose (CAP-12.1): a curve derived from the
    authoritative rule definition deserves more trust than one reconstructed from
    a link, and only the reader can weigh that.
    """

    RULES_API = "RULES_API"
    GENERATOR_URL = "GENERATOR_URL"
    TEMPLATE = "TEMPLATE"


class Tier(str, Enum):
    """How much of the alert expression survived into the chart."""

    THRESHOLD = "THRESHOLD"  # value curve plus a threshold line and its direction
    METRIC = "METRIC"  # the metric itself (only when the name is unique)
    # The raw expression's own result. **Deliberately not called CONDITION**:
    # PromQL's plain comparison *filters series*, it does not return 0/1 (that
    # needs `bool`). Charting it produces a broken line, not a boolean step, and
    # an empty result means "the condition never held here" — which is a useful
    # finding, not a missing metric (D29 / D30).
    EXPRESSION_RESULT = "EXPRESSION_RESULT"


class MetricType(str, Enum):
    COUNTER = "COUNTER"
    GAUGE = "GAUGE"
    UNKNOWN_SUFFIX_GUESS = "UNKNOWN_SUFFIX_GUESS"
    UNKNOWN = "UNKNOWN"


class EvidenceFailureKind(str, Enum):
    """A planned curve did not happen. Never collapsed into one "failed to load".

    The count is **not** the invariant (the first version claimed "exactly
    seven", which was never worth defending). The invariant is that each kind
    maps to a different next action for the reader: "this metric is not in the
    store" sends you to check the name, "the store is unreachable" sends you to
    check the network, and "the condition never held" sends you nowhere at all
    because it is an answer (CAP-12.7a / D30 / D39).
    """

    THANOS_NOT_CONFIGURED = "THANOS_NOT_CONFIGURED"
    THANOS_UNREACHABLE = "THANOS_UNREACHABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    QUERY_SCOPE_UNSAFE = "QUERY_SCOPE_UNSAFE"
    EXPR_UNAVAILABLE = "EXPR_UNAVAILABLE"
    EXPR_UNPARSEABLE = "EXPR_UNPARSEABLE"
    EXPR_AMBIGUOUS = "EXPR_AMBIGUOUS"
    SOURCE_CONFIG_CHANGED = "SOURCE_CONFIG_CHANGED"
    SERIES_LIMIT_EXCEEDED = "SERIES_LIMIT_EXCEEDED"
    # The four ways "nothing came back" can happen. Keeping them apart is the
    # whole point of D30.
    METRIC_NOT_FOUND = "METRIC_NOT_FOUND"
    LABEL_SET_NOT_FOUND = "LABEL_SET_NOT_FOUND"
    NO_SAMPLES_IN_WINDOW = "NO_SAMPLES_IN_WINDOW"
    EXPRESSION_NO_RESULT = "EXPRESSION_NO_RESULT"


class EvidenceWarningKind(str, Enum):
    """The curve **is** there, but something about it was inferred (D39).

    Separate from failures because pairing a working chart with "upstream
    unreachable" misleads the reader about what they are looking at.
    """

    EXPR_FROM_GENERATOR_URL = "EXPR_FROM_GENERATOR_URL"
    RULES_ENDPOINT_UNAVAILABLE = "RULES_ENDPOINT_UNAVAILABLE"
    METRIC_TYPE_UNKNOWN = "METRIC_TYPE_UNKNOWN"
    LABEL_SCHEMA_GUESSED = "LABEL_SCHEMA_GUESSED"
    COUNTER_AS_AUTHORED = "COUNTER_AS_AUTHORED"
    SERIES_TRUNCATED_FOR_DISPLAY = "SERIES_TRUNCATED_FOR_DISPLAY"
    # An auxiliary curve is a guess: "this alert has an `instance`, so maybe node
    # metrics are relevant". A guess that did not pan out is not a fault, and
    # rendering it in red beside a good primary curve is noise the reader has to
    # learn to ignore — which is how real failures start getting ignored too.
    AUXILIARY_NO_DATA = "AUXILIARY_NO_DATA"


# Suffixes Prometheus conventions reserve for cumulative series.
_COUNTER_SUFFIXES = ("_total", "_count", "_sum", "_bucket")


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceNote:
    """One entry in `warnings` or `failures`. Same shape, different meaning."""

    kind: EvidenceFailureKind | EvidenceWarningKind
    detail: str = ""
    subject: str = ""  # which curve it concerns: a template name, or "primary"


# The name this used to have, when failures were the only outlet.
EvidenceFailure = EvidenceNote


@dataclass(frozen=True)
class CurveSeries:
    labels: dict[str, str]
    points: list[tuple[int, float]]


@dataclass(frozen=True)
class Curve:
    curve_id: str
    kind: str  # PRIMARY | AUXILIARY
    title: str
    query: str
    expr_origin: ExprOrigin
    tier: Tier | None
    metric_type: MetricType
    threshold: float | None
    # D33: a threshold line without a direction cannot be read.
    threshold_operator: str | None = None
    display_unit: str = ""
    window_mode: str = "RECENT"  # RECENT | ONSET (D50)
    queried_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    step_seconds: int = 60
    series: list[CurveSeries] = field(default_factory=list)
    # F28: structured facts live with their curve so UI grouping and the F27
    # investigation snapshot share the same identity. Always empty on PRIMARY.

    @property
    def is_empty(self) -> bool:
        return not any(series.points for series in self.series)


@dataclass(frozen=True)
class EvidenceBundle:
    alert_starts_at: datetime | None
    curves: list[Curve]
    warnings: list[EvidenceNote]
    failures: list[EvidenceNote]


@dataclass(frozen=True)
class PrimaryPlan:
    """What tier the primary curve landed on, before the query is composed."""

    tier: Tier
    expr_origin: ExprOrigin
    # THRESHOLD: the sub-expression; EXPRESSION_RESULT: the whole expression.
    expression: str = ""
    # METRIC: the bare metric name to rebuild a selector from.
    metric: str = ""
    threshold: float | None = None
    threshold_operator: str | None = None
    source_expression: str = ""


# ---------------------------------------------------------------------------
# evidence identity
# ---------------------------------------------------------------------------


# 32 hex characters = 128 bits (D49). These go into long-lived investigation
# snapshots; the first version used 12 (48 bits), and paying nothing to remove a
# collision risk is not a trade-off worth thinking about twice.
_ID_HEX = 32


def curve_id(
    *,
    alert_fingerprint: str,
    occurrence_no: int,
    source_config_version: int,
    kind: str,
    query: str,
    window_mode: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """A stable handle for one chart. UI grouping only.

    Includes the **source config version** (D38): the same address pointing at a
    different store is different evidence, and an id that ignored that would let
    a stale citation look current.

    Re-investigating over a moved window yields a different id on purpose — the
    evidence changed, so a conclusion citing it must not silently look like it
    still applies.
    """

    raw = "|".join(
        [
            alert_fingerprint,
            str(occurrence_no),
            str(source_config_version),
            kind,
            query,
            window_mode,
            window_start.isoformat(),
            window_end.isoformat(),
        ]
    )
    return "cv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_ID_HEX]


def fact_id(*, curve: str, series_labels: Mapping[str, str], fact_kind: str) -> str:
    """A handle for one atomic fact: a statistic or interval on one series.

    **This is the only identity a model may cite** (D49 / ADR 0011). Letting it
    reference a whole curve reads like attribution but says nothing checkable —
    "the memory curve supports this" cannot be verified by looking.
    """

    labels = ",".join(f"{key}={series_labels[key]}" for key in sorted(series_labels))
    raw = f"{curve}|{labels}|{fact_kind}"
    return "ft_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_ID_HEX]


# ---------------------------------------------------------------------------
# tier selection
# ---------------------------------------------------------------------------


def plan_primary(
    *, rule_expr: str | None, generator_url: str | None
) -> tuple[PrimaryPlan | None, EvidenceFailure | None]:
    """Pick the expression source, then the tier. Never raises.

    Source order is authority order: the rules endpoint returns a structured rule
    definition, while `generatorURL` is reconstructed from a link that a
    deployment may have broken or omitted. Both stay because the rules endpoint
    is legitimately empty when Thanos Query has no Ruler behind it (ADR 0009).
    """

    expression = (rule_expr or "").strip()
    origin = ExprOrigin.RULES_API
    if not expression:
        expression = (promql.expr_from_generator_url(generator_url) or "").strip()
        origin = ExprOrigin.GENERATOR_URL
    if not expression:
        return None, EvidenceFailure(
            kind=EvidenceFailureKind.EXPR_UNAVAILABLE,
            detail="neither the rules endpoint nor generatorURL carried an expression",
            subject="primary",
        )

    stripped = promql.strip_comparison(expression)
    if stripped is not None:
        return (
            PrimaryPlan(
                tier=Tier.THRESHOLD,
                expr_origin=origin,
                expression=stripped.expression,
                threshold=stripped.threshold,
                threshold_operator=stripped.operator,
                source_expression=expression,
            ),
            None,
        )

    # **Unique metric name only** (D32). Taking `metrics[0]` out of several is an
    # arbitrary choice that can chart something unrelated to the alert. The
    # user's ES alert names one metric across two selectors, so it still lands
    # here.
    metrics = promql.extract_metric_names(expression)
    if len(set(metrics)) == 1:
        return (
            PrimaryPlan(
                tier=Tier.METRIC,
                expr_origin=origin,
                metric=metrics[0],
                source_expression=expression,
            ),
            None,
        )

    # Last tier: run the expression and chart what it returns. Less than a value
    # curve, but it still answers "when did this start going wrong", and no tier
    # may produce an empty chart (CAP-12.4).
    return (
        PrimaryPlan(
            tier=Tier.EXPRESSION_RESULT,
            expr_origin=origin,
            expression=expression,
            source_expression=expression,
        ),
        None,
    )


def classify_metric_type(
    metric: str, metadata: Mapping[str, str] | None
) -> MetricType:
    """Decide whether the curve needs `rate()`.

    When the store has no metadata — common enough — fall back to Prometheus'
    own naming convention and **say so**. Not guessing would draw every counter
    as a line that only ever climbs: a chart that looks entirely normal and means
    nothing, with no way for the reader to tell. Guessing while labelling the
    guess lets them see it (ADR 0009 / CAP-12.5).
    """

    declared = str((metadata or {}).get("type") or "").lower()
    if declared == "counter":
        return MetricType.COUNTER
    if declared in {"gauge", "histogram", "summary", "untyped", "unknown"}:
        # Histograms and summaries are charted through their own `_sum`/`_count`
        # series; the base name behaves like a gauge here.
        return MetricType.GAUGE if declared != "unknown" else MetricType.UNKNOWN
    if any(metric.endswith(suffix) for suffix in _COUNTER_SUFFIXES):
        return MetricType.UNKNOWN_SUFFIX_GUESS
    return MetricType.UNKNOWN


def is_cumulative(metric_type: MetricType) -> bool:
    return metric_type in {MetricType.COUNTER, MetricType.UNKNOWN_SUFFIX_GUESS}


def finalize_primary(
    plan: PrimaryPlan,
    labels: Mapping[str, str],
    metric_type: MetricType = MetricType.UNKNOWN,
    *,
    allowed_label_names: set[str] | None = None,
) -> str:
    """The query actually sent for the primary curve.

    `rate()` is applied **only** on the METRIC tier, where this module composes
    the query itself. Tier 1 and tier 3 forward the author's own expression
    untouched, exactly as user templates are (design §5.1).
    """

    if plan.tier is Tier.METRIC:
        return promql.build_metric_query(
            plan.metric,
            labels,
            is_counter=is_cumulative(metric_type),
            allowed_label_names=allowed_label_names,
            # A label the rule itself compares across several values must not be
            # pinned back to the one that happened to fire — that is exactly the
            # transition the reader came to see (see `labels_with_multiple_values`).
            exclude_label_names=promql.labels_with_multiple_values(
                plan.source_expression
            ),
        )
    return plan.expression


def curve_title(plan: PrimaryPlan, metadata: Mapping[str, str] | None) -> str:
    """Prefer the store's own help text; it is written for humans already."""

    help_text = str((metadata or {}).get("help") or "").strip()
    if help_text:
        return help_text
    if plan.tier is Tier.METRIC and plan.metric:
        return plan.metric
    if plan.tier is Tier.THRESHOLD:
        return "告警表达式的取值"
    # Not "whether the condition held" — a plain comparison filters series rather
    # than returning a boolean, so that phrasing would describe a chart nobody
    # is looking at (D29).
    return "告警表达式的返回结果"


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------


def parse_matrix(payload: Mapping[str, Any] | None) -> list[CurveSeries]:
    """Turn a `query_range` matrix into series, dropping unusable samples.

    A sample whose value is `NaN`, `+Inf` or unparseable is skipped rather than
    charted as zero: a fabricated zero is indistinguishable from a real one.
    """

    if not isinstance(payload, Mapping):
        return []
    result = payload.get("result")
    if not isinstance(result, list):
        return []

    series: list[CurveSeries] = []
    for item in result:
        if not isinstance(item, Mapping):
            continue
        raw_labels = item.get("metric")
        labels = (
            {str(key): str(value) for key, value in raw_labels.items()}
            if isinstance(raw_labels, Mapping)
            else {}
        )
        points: list[tuple[int, float]] = []
        for sample in item.get("values") or []:
            if not isinstance(sample, Sequence) or len(sample) < 2:
                continue
            try:
                timestamp = int(float(sample[0]))
                value = float(sample[1])
            except (TypeError, ValueError):
                continue
            if value != value or value in (float("inf"), float("-inf")):
                continue
            points.append((timestamp, value))
        series.append(CurveSeries(labels=labels, points=points))
    return series


def curve_from_matrix(
    *,
    curve_id_value: str,
    kind: str,
    title: str,
    query: str,
    expr_origin: ExprOrigin,
    tier: Tier | None,
    metric_type: MetricType,
    threshold: float | None,
    window: QueryWindow,
    payload: Mapping[str, Any] | None,
    threshold_operator: str | None = None,
    display_unit: str = "",
    window_mode: str = "RECENT",
    queried_at: datetime | None = None,
) -> Curve:
    """Assemble one curve, enforcing the series ceiling before anything is kept.

    Refusing beats trimming: the first 20 of 500 series would draw a chart that
    looks fine and describes nothing.
    """

    series = parse_matrix(payload)
    assert_series_count_within_budget(len(series))
    return Curve(
        curve_id=curve_id_value,
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
        window_start=window.start,
        window_end=window.end,
        step_seconds=window.step_seconds,
        series=series,
    )


def build_bundle(
    *,
    alert_starts_at: datetime | None,
    curves: Sequence[Curve],
    warnings: Sequence[EvidenceNote] = (),
    failures: Sequence[EvidenceNote] = (),
) -> EvidenceBundle:
    """Primary first, then auxiliary in the order they were planned."""

    ordered = [curve for curve in curves if curve.kind == "PRIMARY"]
    ordered += [curve for curve in curves if curve.kind != "PRIMARY"]
    return EvidenceBundle(
        alert_starts_at=alert_starts_at,
        curves=list(ordered),
        warnings=list(warnings),
        failures=list(failures),
    )


def classify_empty_result(
    *,
    tier: Tier | None,
    metric: str,
    known_metrics: set[str] | None,
    series_found: int,
) -> EvidenceFailureKind:
    """Say *why* nothing came back. Four answers, four different next steps (D30).

    The first version reported `METRIC_NOT_FOUND` for all of them, which sent the
    reader to check a metric name even when the real answer was "this condition
    never held in this window" — information, not a fault.

    `METRIC_NOT_FOUND` is only used when absence can actually be **proven**, i.e.
    a metric catalogue was fetched and the name is not in it. Without that
    catalogue the honest answer is the weaker one.
    """

    if tier is Tier.EXPRESSION_RESULT:
        # The expression ran and matched nothing. For a comparison that means the
        # condition was never true here — an answer in its own right.
        return EvidenceFailureKind.EXPRESSION_NO_RESULT
    if series_found > 0:
        # The selector resolves to series; they just have no samples in range.
        return EvidenceFailureKind.NO_SAMPLES_IN_WINDOW
    if known_metrics is not None and metric:
        if metric not in known_metrics:
            return EvidenceFailureKind.METRIC_NOT_FOUND
        return EvidenceFailureKind.LABEL_SET_NOT_FOUND
    # No catalogue to check against: do not claim the metric is missing.
    return EvidenceFailureKind.LABEL_SET_NOT_FOUND


def rule_expression_for(
    rules: Sequence[Mapping[str, Any]],
    alertname: str,
    alert_labels: Mapping[str, str] | None = None,
) -> tuple[str | None, EvidenceFailureKind | None]:
    """Find an alert's expression, refusing to guess when several could match.

    The same alert name defined once per cluster or per team is ordinary, and the
    first version took whichever came back first — which made the chart depend on
    the upstream's response order (D31).

    **Duplicates are not candidates.** Thanos Query fans this endpoint out across
    every Prometheus behind it, so an HA pair returns the same rule twice and a
    three-replica setup three times. Refusing to choose there would be refusing a
    choice that does not exist — and it is the common case, not the exotic one.
    Distinct *expressions* are what makes a lookup ambiguous, so that is what is
    counted; replica labels and whitespace are not part of the identity.

    Only when the expressions genuinely differ do we disambiguate on the rule's
    own static labels: a rule pinning `cluster="west"` cannot be the source of an
    alert carrying `cluster="east"`. If that still leaves more than one, we say so
    rather than pick — `EXPR_AMBIGUOUS` is a readable outcome, a silently wrong
    curve is not.
    """

    target = (alertname or "").strip()
    if not target:
        return None, None

    candidates = [
        rule
        for rule in rules
        if isinstance(rule, Mapping)
        and str(rule.get("name") or "").strip() == target
        and str(rule.get("query") or "").strip()
    ]
    if not candidates:
        return None, None

    if len({_expression_identity(rule) for rule in candidates}) == 1:
        # One expression, however many copies of it came back.
        return str(candidates[0].get("query") or "").strip(), None

    if len(candidates) > 1 and alert_labels:
        compatible = []
        for rule in candidates:
            static = rule.get("labels")
            if not isinstance(static, Mapping):
                compatible.append(rule)
                continue
            if all(
                str(alert_labels.get(key, "")) == str(value)
                for key, value in static.items()
            ):
                compatible.append(rule)
        if compatible:
            candidates = compatible
    if len({_expression_identity(rule) for rule in candidates}) > 1:
        return None, EvidenceFailureKind.EXPR_AMBIGUOUS
    return str(candidates[0].get("query") or "").strip(), None


def _expression_identity(rule: Mapping[str, Any]) -> str:
    """What makes two rule definitions the *same* rule.

    Whitespace only: an expression is identified by what it computes, and the
    fan-out copies can differ in formatting without differing in meaning. Labels
    are deliberately excluded — replica labels differ per Prometheus and say
    nothing about which rule this is.
    """

    return " ".join(str(rule.get("query") or "").split())


__all__ = [
    "MAX_SERIES_PER_QUERY",
    "Curve",
    "EvidenceNote",
    "EvidenceWarningKind",
    "classify_empty_result",
    "curve_id",
    "fact_id",
    "CurveSeries",
    "EvidenceBundle",
    "EvidenceFailure",
    "EvidenceFailureKind",
    "ExprOrigin",
    "MetricType",
    "PrimaryPlan",
    "Tier",
    "build_bundle",
    "classify_metric_type",
    "curve_from_matrix",
    "curve_title",
    "finalize_primary",
    "is_cumulative",
    "parse_matrix",
    "plan_primary",
    "rule_expression_for",
]
