"""Pure, explainable scoring for resolved occurrence history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

MINIMUM_SIMILARITY_SCORE = 50
MAX_SIMILAR_OCCURRENCES = 10


@dataclass(frozen=True, slots=True)
class SimilarityProfileV1:
    service_id: int | None
    primary_alertname: str | None
    aggregation_rule_id: int | None
    group_labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SimilarityReasonV1:
    kind: str
    field: str
    value: str
    points: int


@dataclass(frozen=True, slots=True)
class SimilarityScoreV1:
    score: int
    reasons: tuple[SimilarityReasonV1, ...]


def score_similar_occurrence(
    current: SimilarityProfileV1, candidate: SimilarityProfileV1
) -> SimilarityScoreV1:
    reasons: list[SimilarityReasonV1] = []
    if current.service_id is not None and current.service_id == candidate.service_id:
        reasons.append(
            SimilarityReasonV1("SERVICE", "service_id", str(current.service_id), 40)
        )
    if (
        current.primary_alertname
        and current.primary_alertname == candidate.primary_alertname
    ):
        reasons.append(
            SimilarityReasonV1(
                "PRIMARY_ALERTNAME",
                "alertname",
                current.primary_alertname,
                25,
            )
        )
    if (
        current.aggregation_rule_id is not None
        and current.aggregation_rule_id == candidate.aggregation_rule_id
    ):
        reasons.append(
            SimilarityReasonV1(
                "AGGREGATION_RULE",
                "aggregation_rule_id",
                str(current.aggregation_rule_id),
                15,
            )
        )
    matching_labels = sorted(
        (name, value)
        for name, value in current.group_labels.items()
        if candidate.group_labels.get(name) == value
    )[:4]
    reasons.extend(
        SimilarityReasonV1("GROUP_LABEL", name, value, 5)
        for name, value in matching_labels
    )
    return SimilarityScoreV1(sum(item.points for item in reasons), tuple(reasons))
