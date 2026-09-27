"""The investigation's three structural guarantees, each asserted not requested.

The prompt asks for a lot. None of it is trusted: fact ids are checked against
the frozen snapshot, verdict rules are pydantic validators, and an answer that
fails twice is never shown. What the wording buys is a *likely* good answer;
what these buy is that a bad one cannot reach the reader.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.providers.model.base import ModelCallError, ModelFailureKind, ModelReply
from app.providers.model.fake import FakeModelClient
from app.services.handling_digest import HandlingDigest
from app.services.investigation import (
    MAX_INVESTIGATION_CALLS,
    MetricFact,
    NotEnoughEvidence,
    build_investigation_input,
    run_investigation,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
FACT = MetricFact(fact_id="ft_1", statement="yellow 从 0 变 1 并保持 22 分钟")


def _input(**overrides):
    base = dict(
        alertname="ESClusterYellow",
        labels={"cluster": "nonprod", "color": "yellow"},
        annotations={"summary": "集群健康度变黄"},
        started_at=NOW,
        metric_facts=[FACT],
        digest=None,
    )
    base.update(overrides)
    return build_investigation_input(**base)


def _answer(**overrides) -> str:
    hypothesis = {
        "statement": "分片未分配导致集群变黄",
        "verdict": "SUPPORTED",
        "supporting_fact_ids": ["ft_1"],
        "contradicting_fact_ids": [],
        "missing_evidence": [],
        "recommendations": [
            {"kind": "NEXT_CHECK", "text": "看 _cluster/allocation/explain"}
        ],
    }
    hypothesis.update(overrides)
    return json.dumps({"hypotheses": [hypothesis]}, ensure_ascii=False)


# --- what the model is allowed to see --------------------------------------


def test_the_alert_prose_is_included_because_this_call_has_no_tools() -> None:
    """The opposite rule from query authoring, and for the opposite reason.

    Annotations are the whole point of this call. It can afford them because it
    has nothing to act with: no tools, no queries, only text out.
    """
    payload = _input().as_prompt_payload()
    assert payload["alert"]["annotations"]["summary"] == "集群健康度变黄"


def test_the_generator_url_never_reaches_the_model_even_as_an_annotation() -> None:
    """It carries internal hostnames and buys nothing the annotations do not."""
    payload = _input(
        annotations={
            "summary": "ok",
            "generatorURL": "http://prometheus-0.internal:9090/graph?g0.expr=up",
        }
    ).as_prompt_payload()
    serialised = json.dumps(payload, ensure_ascii=False)
    assert "prometheus-0.internal" not in serialised
    assert "generatorURL" not in serialised


def test_an_enormous_annotation_is_bounded_before_it_is_sent() -> None:
    """"However long it happens to be" is not a request size anyone chose."""
    payload = _input(annotations={"summary": "x" * 50_000}).as_prompt_payload()
    assert len(payload["alert"]["annotations"]["summary"]) < 5_000


# --- the minimum evidence gate ---------------------------------------------


@pytest.mark.asyncio
async def test_no_metric_fact_means_no_call_at_all() -> None:
    """D52: paying a model to paraphrase the annotation is worse than silence,
    because the paraphrase looks like analysis."""
    fake = FakeModelClient()
    with pytest.raises(NotEnoughEvidence):
        await run_investigation(
            model=fake, investigation_input=_input(metric_facts=[])
        )
    assert fake.calls == [], "没有证据却已经花了钱"


# --- the model cannot cite what it was not given ---------------------------


@pytest.mark.asyncio
async def test_an_invented_fact_id_fails_the_result() -> None:
    """This is what makes "attributed to specific evidence" mean something.

    Without it the model can write a fluent conclusion and hang an id-shaped
    string off it, and the citation reads exactly as well as a real one.
    """
    fake = FakeModelClient()
    fake.replies = [
        ModelReply(text=_answer(supporting_fact_ids=["ft_does_not_exist"])),
        ModelReply(text=_answer(supporting_fact_ids=["ft_does_not_exist"])),
    ]

    outcome = await run_investigation(model=fake, investigation_input=_input())

    assert outcome.ok is False
    assert outcome.failure == "FAILED_VALIDATION"


@pytest.mark.asyncio
async def test_a_well_formed_answer_is_accepted_in_one_call() -> None:
    fake = FakeModelClient()
    fake.replies = [ModelReply(text=_answer(), model_name="gpt-x")]

    outcome = await run_investigation(model=fake, investigation_input=_input())

    assert outcome.ok is True
    assert outcome.model_calls == 1
    assert outcome.result.hypotheses[0].verdict == "SUPPORTED"
    assert outcome.prompt_version.startswith("investigation/")


# --- the one allowed second call, and its limit ----------------------------


@pytest.mark.asyncio
async def test_a_malformed_answer_gets_exactly_one_repair_attempt() -> None:
    """Not a retry of the same question: the answer arrived, in the wrong shape.

    Asking for the same content in the required shape is a different request —
    which is why it does not violate "never retry a billed call" (D45).
    """
    fake = FakeModelClient()
    fake.replies = [
        ModelReply(text="这里是一段自由发挥的中文"),
        ModelReply(text=_answer()),
    ]

    outcome = await run_investigation(model=fake, investigation_input=_input())

    assert outcome.ok is True
    assert outcome.model_calls == 2
    assert len(fake.calls) == MAX_INVESTIGATION_CALLS


@pytest.mark.asyncio
async def test_it_never_calls_a_third_time() -> None:
    fake = FakeModelClient()
    fake.replies = [ModelReply(text="不是 JSON")] * 5

    outcome = await run_investigation(model=fake, investigation_input=_input())

    assert outcome.ok is False
    assert outcome.model_calls == MAX_INVESTIGATION_CALLS
    assert len(fake.calls) == MAX_INVESTIGATION_CALLS


@pytest.mark.asyncio
async def test_a_billed_transport_failure_stops_rather_than_being_absorbed() -> None:
    """Only the caller knows whether to record it as billed; swallowing the
    error here would make that decision invisible."""
    fake = FakeModelClient(replies=[ModelCallError(ModelFailureKind.TIMEOUT)])

    with pytest.raises(ModelCallError) as excinfo:
        await run_investigation(model=fake, investigation_input=_input())
    assert excinfo.value.possibly_billed is True


# --- what an unusable answer must not do -----------------------------------


@pytest.mark.asyncio
async def test_the_unusable_raw_text_is_kept_but_marked_not_for_display() -> None:
    """D44: an unattributed fluent conclusion is what the structure exists to
    keep away from the reader, and a "not structured" label does not stop
    anyone reading it. Stored so "why does this model keep failing" is
    answerable; never rendered, never returned by the API.
    """
    fake = FakeModelClient()
    fake.replies = [ModelReply(text="内存不足导致的，重启就好了")] * 2

    outcome = await run_investigation(model=fake, investigation_input=_input())

    assert outcome.ok is False
    assert outcome.result is None
    assert "重启就好了" in outcome.raw_text


@pytest.mark.asyncio
async def test_the_snapshot_records_exactly_what_the_model_saw(session=None) -> None:
    """D48: "only the query and the window" cannot answer "what did it read"."""
    fake = FakeModelClient()
    fake.replies = [ModelReply(text=_answer())]

    digest = HandlingDigest(
        group_key="g", has_history=True, occurrences_in_window=7,
        median_duration_seconds=240, longest_duration_seconds=900,
        conclusion_counts={"FALSE_ALARM": 3},
    )
    outcome = await run_investigation(
        model=fake, investigation_input=_input(digest=digest)
    )

    assert outcome.snapshot["alert"]["alertname"] == "ESClusterYellow"
    assert outcome.snapshot["metric_facts"][0]["fact_id"] == "ft_1"
    assert any(
        "误报" in str(item["statement"]) or "FALSE_ALARM" in str(item["statement"])
        for item in outcome.snapshot["handling_history"]
    )


# --- curves become statistics, never raw points ----------------------------


class _Series:
    def __init__(self, labels, points):
        self.labels = labels
        self.points = points


class _Curve:
    def __init__(self, series, **kw):
        self.curve_id = kw.get("curve_id", "cv_1")
        self.title = kw.get("title", "ES 集群健康度")
        self.display_unit = kw.get("display_unit", "")
        self.threshold = kw.get("threshold")
        self.threshold_operator = kw.get("threshold_operator")
        self.series = series


def test_the_model_gets_statistics_not_hundreds_of_samples() -> None:
    """It would be doing arithmetic otherwise — badly, confidently, and in a
    tone indistinguishable from doing it well (ADR 0011)."""
    from app.services.investigation import metric_facts_from_curves

    points = [(i, float(i % 3)) for i in range(500)]
    facts = metric_facts_from_curves([_Curve([_Series({"pod": "es-0"}, points)])])

    assert len(facts) == 1
    statement = facts[0].statement
    assert "pod=es-0" in statement
    # No sample list anywhere in what the model will read.
    assert str(points[10]) not in statement
    assert len(statement) < 300


def test_threshold_crossings_are_counted_by_code() -> None:
    from app.services.investigation import metric_facts_from_curves

    values = [0.0, 2.0, 0.0, 2.0]
    facts = metric_facts_from_curves(
        [
            _Curve(
                [_Series({"pod": "p"}, list(enumerate(values)))],
                threshold=1.0,
                threshold_operator=">",
            )
        ]
    )
    assert "穿越 3 次" in facts[0].statement


def test_omitted_series_are_declared_so_no_claim_covers_them() -> None:
    """Otherwise the model states something about the whole curve having seen
    a fifth of it."""
    from app.services.investigation import MAX_SERIES_PER_CURVE, metric_facts_from_curves

    series = [
        _Series({"pod": f"p{i}"}, [(0, 1.0), (1, 2.0)])
        for i in range(MAX_SERIES_PER_CURVE + 3)
    ]
    facts = metric_facts_from_curves([_Curve(series)])

    partial = [f for f in facts if f.fact_id.endswith("_partial")]
    assert len(partial) == 1
    assert "不要对整体下断言" in partial[0].statement


def test_a_curve_that_drew_nothing_contributes_no_fact() -> None:
    """What makes the minimum-evidence gate mean something: no fact, no call."""
    from app.services.investigation import metric_facts_from_curves

    assert metric_facts_from_curves([_Curve([])]) == []
    assert metric_facts_from_curves([]) == []
