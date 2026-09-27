"""One investigation: freeze the evidence, ask once, check what comes back.

The shape is the notification domain's, for the same reason: everything
decidable happens before the network call, the call is the only I/O, and nothing
is trusted on the way back.

Three properties this module exists to hold, none of which is a prompt request:

- **The model cannot cite evidence that was not given to it.** Every `fact_id`
  in the answer is checked against the frozen snapshot, and one that misses
  fails the whole result. This is what makes "attributed to specific evidence"
  (D22) mean something rather than read like it does.
- **The numbers are ours.** Statistics are computed by code and handed over as
  facts (ADR 0011). Models count badly and confidently, and a wrong count reads
  exactly like a right one.
- **Nothing is retried that might have been billed** (D45). One call; a second
  is allowed *only* to repair a structurally invalid answer, because that one is
  answering a different question ("say it again in the required shape").

`generatorURL` and the raw payload never reach the model: they carry internal
hostnames, and unlike the annotations they buy nothing (D51).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from app.prompts import (
    INVESTIGATION_PROMPT,
    INVESTIGATION_VERSION,
    investigation_response_format,
)
from app.providers.model.base import ModelCallError, ModelClient
from app.services.handling_digest import HandlingDigest, digest_facts
from app.services.investigation_contract import (
    InvestigationContractError,
    InvestigationResult,
    validate_investigation_result,
)

#: One call, plus at most one repair. The repair is not a retry of the same
#: question -- the first answer arrived, it simply was not in the required
#: shape -- so it does not violate "never retry a billed request" (D45).
MAX_INVESTIGATION_CALLS = 2

#: Annotations are free text written by the monitored system. They are the whole
#: reason this call is worth making, and also the reason it has no tools.
MAX_ANNOTATION_LENGTH = 2000
MAX_ANNOTATIONS = 12
MAX_LABELS = 40


@dataclass(frozen=True)
class MetricFact:
    """One checkable statement about a curve, computed by code."""

    fact_id: str
    statement: str
    curve_id: str = ""
    fact_kind: str = ""
    # Structured code output is frozen beside the sentence when a fact type
    # needs fields beyond the human-readable statement.
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InvestigationInput:
    """Exactly what the model is shown. The absences are deliberate.

    There is no `generator_url` and no `raw_payload` field: both carry internal
    hostnames and neither adds anything the annotations do not (D51). Unlike the
    query-authoring call, annotations **are** included -- this call has no tools,
    so there is no action for text to reach.
    """

    alertname: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    started_at: str
    metric_facts: tuple[MetricFact, ...] = ()
    history_facts: tuple[Mapping[str, Any], ...] = ()

    def fact_ids(self) -> set[str]:
        return {fact.fact_id for fact in self.metric_facts} | {
            str(item["fact_id"]) for item in self.history_facts
        }

    def as_prompt_payload(self) -> dict[str, Any]:
        return {
            "alert": {
                "alertname": self.alertname,
                "labels": dict(sorted(self.labels.items())),
                "annotations": dict(sorted(self.annotations.items())),
                "started_at": self.started_at,
            },
            "metric_facts": [
                {
                    "fact_id": fact.fact_id,
                    "statement": fact.statement,
                    **({"curve_id": fact.curve_id} if fact.curve_id else {}),
                    **({"fact_kind": fact.fact_kind} if fact.fact_kind else {}),
                    **({"data": dict(fact.data)} if fact.data else {}),
                }
                for fact in self.metric_facts
            ],
            "handling_history": [dict(item) for item in self.history_facts],
        }


class NotEnoughEvidence(ValueError):
    """The minimum evidence gate (D52).

    With no metric fact that survived validation, the only thing left to reason
    from is the annotation -- and paying a model to paraphrase text the user
    wrote is worse than saying nothing, because the paraphrase looks like
    analysis.
    """


@dataclass
class InvestigationOutcome:
    ok: bool
    result: InvestigationResult | None = None
    failure: str = ""
    detail: str = ""
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_name: str = ""
    prompt_version: str = INVESTIGATION_VERSION
    #: Stored, never rendered and never returned by the API (D44). Kept so
    #: "why does this model keep failing validation" is answerable.
    raw_text: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)


def _clamp(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def build_investigation_input(
    *,
    alertname: str,
    labels: Mapping[str, str],
    annotations: Mapping[str, str],
    started_at: datetime,
    metric_facts: Sequence[MetricFact],
    digest: HandlingDigest | None,
) -> InvestigationInput:
    """The single doorway into the investigation prompt.

    Bounded here rather than at each call site: an annotation is arbitrary text
    from the monitored system, and "however long it happens to be" is not a
    request size anyone chose.
    """

    trimmed_labels = dict(sorted(labels.items())[:MAX_LABELS])
    trimmed_annotations = {
        key: _clamp(value, MAX_ANNOTATION_LENGTH)
        for key, value in sorted(annotations.items())[:MAX_ANNOTATIONS]
        # `generatorURL` sometimes appears as an annotation too; it is excluded
        # wherever it turns up, not only where it was expected.
        if key not in {"generatorURL", "generator_url"}
    }
    return InvestigationInput(
        alertname=alertname,
        labels=trimmed_labels,
        annotations=trimmed_annotations,
        started_at=started_at.isoformat(),
        metric_facts=tuple(metric_facts),
        history_facts=tuple(digest_facts(digest)) if digest is not None else (),
    )


async def run_investigation(
    *, model: ModelClient, investigation_input: InvestigationInput
) -> InvestigationOutcome:
    """One structured call, then validation. No tools, no agent loop (D42).

    A `ModelCallError` propagates rather than being converted here: only the
    caller knows whether to record it as billed, and swallowing it would make
    that decision invisible.
    """

    if not investigation_input.metric_facts:
        raise NotEnoughEvidence("no validated metric fact to reason from")

    payload = investigation_input.as_prompt_payload()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": INVESTIGATION_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        },
    ]
    available = investigation_input.fact_ids()

    calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    model_name = ""
    last_text = ""
    last_error = ""

    while calls < MAX_INVESTIGATION_CALLS:
        reply = await model.complete(
            messages=messages,
            tools=(),  # No tools, ever. This call reads the alert's prose.
            response_format=investigation_response_format(),
        )
        calls += 1
        prompt_tokens += reply.prompt_tokens
        completion_tokens += reply.completion_tokens
        model_name = reply.model_name or model_name
        last_text = reply.text

        try:
            result = validate_investigation_result(
                reply.text, available_fact_ids=available
            )
        except InvestigationContractError as exc:
            last_error = str(exc)[:200]
            if calls >= MAX_INVESTIGATION_CALLS:
                break
            # Not a retry of the same question: the answer arrived, it was the
            # wrong shape. Asking for the same content in the required shape is
            # a different request, and it is the only second call allowed.
            messages.append({"role": "assistant", "content": reply.text or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That answer did not satisfy the required structure: "
                        f"{last_error}. Answer again in the required JSON shape, "
                        "citing only fact_ids that were given to you."
                    ),
                }
            )
            continue

        return InvestigationOutcome(
            ok=True,
            result=result,
            model_calls=calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_name=model_name,
            raw_text=last_text,
            snapshot=payload,
        )

    # Structurally invalid twice. The raw text is stored but never rendered and
    # never returned by the API (D44): an unattributed fluent conclusion is
    # exactly what this whole structure exists to keep away from the reader,
    # and labelling it "unstructured" does not stop anyone reading it.
    return InvestigationOutcome(
        ok=False,
        failure="FAILED_VALIDATION",
        detail=last_error,
        model_calls=calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model_name=model_name,
        raw_text=last_text,
        snapshot=payload,
    )


__all__ = [
    "MAX_INVESTIGATION_CALLS",
    "MAX_SERIES_PER_CURVE",
    "metric_facts_from_curves",
    "InvestigationInput",
    "InvestigationOutcome",
    "MetricFact",
    "NotEnoughEvidence",
    "build_investigation_input",
    "run_investigation",
]


# ---------------------------------------------------------------------------
# Curves -> citable facts
# ---------------------------------------------------------------------------

#: Per curve. Five series is already more than a sentence can honestly
#: summarise, and the model must not generalise over the ones it cannot see.
MAX_SERIES_PER_CURVE = 5


def _series_name(labels: Mapping[str, str]) -> str:
    for key in ("pod", "instance", "node", "container", "namespace", "cluster", "job"):
        if labels.get(key):
            return f"{key}={labels[key]}"
    parts = [f"{k}={v}" for k, v in sorted(labels.items()) if k != "__name__"]
    return parts[0] if parts else "series"


def metric_facts_from_curves(
    curves: Sequence[Any], *, alert_starts_at: datetime | None = None
) -> list[MetricFact]:
    """Turn drawn curves into statements the model may cite.

    **Statistics, never raw points.** Two reasons, and the second is the one
    that matters: a few hundred samples per series would dominate the request,
    and a model asked to read them would be doing arithmetic — which it does
    badly, confidently, and in a tone indistinguishable from doing it well
    (ADR 0011). The numbers are computed here and handed over as facts.

    Curves that failed to draw contribute nothing. That is what makes the
    minimum-evidence gate meaningful: no fact means no call.
    """

    facts: list[MetricFact] = []
    for curve in curves:
        series_list = list(getattr(curve, "series", []) or [])
        shown = series_list[:MAX_SERIES_PER_CURVE]
        title = str(getattr(curve, "title", "") or "曲线")
        unit = str(getattr(curve, "display_unit", "") or "")
        threshold = getattr(curve, "threshold", None)
        operator = getattr(curve, "threshold_operator", None)

        for index, series in enumerate(shown):
            points = [
                (int(x), float(y))
                for x, y in (getattr(series, "points", []) or [])
                if y == y  # NaN is never a statistic worth reporting
            ]
            if not points:
                continue
            values = [value for _, value in points]
            first, last = values[0], values[-1]
            low, high = min(values), max(values)
            name = _series_name(dict(getattr(series, "labels", {}) or {}))

            statement = (
                f"{title}·{name}：起 {first:.4g}{unit}，末 {last:.4g}{unit}，"
                f"区间 {low:.4g}–{high:.4g}{unit}"
            )
            if threshold is not None and operator:
                crossings = sum(
                    1
                    for previous, current in zip(values, values[1:])
                    if _crosses(previous, current, threshold, operator)
                )
                statement += f"；阈值 {operator} {threshold:.4g}，穿越 {crossings} 次"
            facts.append(
                MetricFact(
                    fact_id=f"ft_{getattr(curve, 'curve_id', 'c')}_{index}",
                    statement=statement,
                    curve_id=str(getattr(curve, "curve_id", "")),
                )
            )

        omitted = len(series_list) - len(shown)
        if omitted > 0:
            # Stated so the model cannot make a claim about the whole curve
            # while having seen a fifth of it.
            facts.append(
                MetricFact(
                    fact_id=f"ft_{getattr(curve, 'curve_id', 'c')}_partial",
                    statement=f"{title}：另有 {omitted} 条序列未纳入，不要对整体下断言",
                    curve_id=str(getattr(curve, "curve_id", "")),
                )
            )
    return facts


def _crosses(previous: float, current: float, threshold: float, operator: str) -> bool:
    def side(value: float) -> bool:
        if operator in {">", ">="}:
            return value >= threshold
        if operator in {"<", "<="}:
            return value <= threshold
        return value == threshold

    return side(previous) != side(current)
