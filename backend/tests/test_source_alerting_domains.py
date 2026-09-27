"""Pure-domain characterization for source collection and grouping."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domains.alerting.models import (
    AggregationRule,
    Matcher,
    MatcherOperator,
    choose_aggregation,
    normalize_alert,
)
from app.domains.sources.models import (
    EndpointObservation,
    EndpointSnapshot,
    PollCompleteness,
    merge_endpoint_observations,
)


def _endpoint(position: int) -> EndpointSnapshot:
    return EndpointSnapshot(
        source_id="src-a",
        source_version=3,
        position=position,
        canonical_url=f"https://am-{position}.invalid",
        auth_kind="NONE",
        username="",
        secret="",
        timeout_seconds=10.0,
    )


def _alert(
    fingerprint: str | None,
    *,
    summary: str = "target down",
    updated_at: str = "2026-08-11T01:00:00Z",
) -> dict[str, object]:
    value: dict[str, object] = {
        "labels": {
            "alertname": "TargetDown",
            "severity": "warning",
            "cluster": "cluster-a",
        },
        "annotations": {"summary": summary},
        "startsAt": "2026-08-11T00:00:00Z",
        "updatedAt": updated_at,
    }
    if fingerprint is not None:
        value["fingerprint"] = fingerprint
    return value


def test_merge_preserves_ha_union_identity_divergence_and_completeness() -> None:
    first = _endpoint(0)
    second = _endpoint(1)
    merged = merge_endpoint_observations(
        (
            EndpointObservation(
                endpoint=first,
                status="SUCCESS",
                alerts=(_alert("same", summary="old"), _alert("only-a")),
                duration_ms=4,
            ),
            EndpointObservation(
                endpoint=second,
                status="SUCCESS",
                alerts=(
                    _alert(
                        "same",
                        summary="new",
                        updated_at="2026-08-11T01:01:00Z",
                    ),
                ),
                duration_ms=5,
            ),
        )
    )

    assert merged.completeness is PollCompleteness.COMPLETE
    assert [item.identity for item in merged.alerts] == ["only-a", "same"]
    same = merged.alerts[1]
    assert same.endpoint_positions == (0, 1)
    assert same.raw["annotations"] == {"summary": "new"}
    assert merged.safe_error_codes == ("PAYLOAD_DIVERGENCE",)

    partial = merge_endpoint_observations(
        (
            EndpointObservation(first, "SUCCESS", (_alert(None),), 4),
            EndpointObservation(
                second,
                "TIMEOUT",
                (),
                10,
                safe_error_code="ENDPOINT_TIMEOUT",
            ),
        )
    )
    assert partial.completeness is PollCompleteness.PARTIAL
    assert partial.alerts[0].identity.startswith("labels:")
    assert partial.safe_error_codes == ("ENDPOINT_TIMEOUT", "PARTIAL_POLL")


def test_normalization_and_rule_choice_are_source_bound_and_missing_safe() -> None:
    alert = normalize_alert(_alert("fp-1"), source_id="src-a")
    selected = AggregationRule(
        id=20,
        name="targets by cluster",
        priority=10,
        enabled=True,
        matchers=(Matcher("alertname", MatcherOperator.EQUALS, "TargetDown"),),
        group_by_labels=("cluster", "namespace"),
        source_ids=("src-a",),
        version=2,
    )
    later = AggregationRule(
        id=10,
        name="later by priority",
        priority=20,
        enabled=True,
        matchers=(),
        group_by_labels=("cluster",),
        source_ids=(),
        version=1,
    )

    decision = choose_aggregation(alert, (later, selected))

    assert decision.rule_id == 20
    assert decision.missing_labels == ("namespace",)
    assert decision.group_key.endswith("fallback=fp-1")
    assert "source=src-a" in decision.group_key
    assert choose_aggregation(
        normalize_alert(_alert("fp-1"), source_id="src-b"),
        (selected,),
    ).rule_id is None


def test_normalization_has_no_environment_routing_field() -> None:
    raw = _alert("fp-env")
    labels = dict(raw["labels"])  # type: ignore[arg-type]
    labels["environment"] = "production"
    raw["labels"] = labels

    normalized = normalize_alert(raw, source_id="src-a")

    assert "environment" not in normalized.route_fields
    assert normalized.observed_at == datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
