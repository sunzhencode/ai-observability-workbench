"""Structured result contract shared by channel tests and investigations.

The investigation orchestrator is still a later F27 task.  The contract lives
on its own now because D46 requires a channel test to prove that a configured
service can produce the *real shape*, not merely return HTTP 200.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Verdict = Literal["SUPPORTED", "SYMPTOM", "DISPROVEN", "BLOCKED"]
RecommendationKind = Literal["NEXT_CHECK", "MITIGATION_CANDIDATE"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Recommendation(ContractModel):
    kind: RecommendationKind
    text: str = Field(min_length=1, max_length=1000)


class Hypothesis(ContractModel):
    statement: str = Field(min_length=1, max_length=1000)
    verdict: Verdict
    # OpenAI strict JSON schema requires every declared property to be listed in
    # ``required``. Empty arrays are explicit rather than omitted so the same
    # contract works with strict services and deterministic pydantic validation.
    supporting_fact_ids: list[str] = Field(max_length=50)
    contradicting_fact_ids: list[str] = Field(max_length=50)
    missing_evidence: list[str] = Field(max_length=20)
    recommendations: list[Recommendation] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_verdict_evidence(self) -> "Hypothesis":
        if self.verdict in {"SUPPORTED", "SYMPTOM"} and not self.supporting_fact_ids:
            raise ValueError(f"{self.verdict} requires supporting facts")
        if self.verdict == "DISPROVEN":
            if not self.contradicting_fact_ids:
                raise ValueError("DISPROVEN requires contradicting facts")
            if self.recommendations:
                raise ValueError("DISPROVEN cannot carry recommendations")
        if self.verdict == "BLOCKED" and not self.missing_evidence:
            raise ValueError("BLOCKED requires missing evidence")
        if self.verdict != "SUPPORTED" and any(
            item.kind == "MITIGATION_CANDIDATE" for item in self.recommendations
        ):
            raise ValueError("mitigation candidates require SUPPORTED")
        return self


class InvestigationResult(ContractModel):
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=10)


class InvestigationContractError(ValueError):
    """Fixed error type whose text never includes model output."""


def validate_investigation_result(
    raw_text: str,
    *,
    available_fact_ids: set[str] | frozenset[str],
) -> InvestigationResult:
    """Parse the result and bind every cited fact to the supplied snapshot."""

    try:
        result = InvestigationResult.model_validate_json(raw_text)
    except (ValidationError, ValueError, TypeError):
        raise InvestigationContractError("investigation contract is invalid") from None

    allowed = set(available_fact_ids)
    cited = {
        fact_id
        for hypothesis in result.hypotheses
        for fact_id in (
            *hypothesis.supporting_fact_ids,
            *hypothesis.contradicting_fact_ids,
        )
    }
    if not cited <= allowed:
        raise InvestigationContractError("investigation fact ownership is invalid")
    return result


def structured_response_format() -> dict:
    """OpenAI-compatible strict JSON schema for the same pydantic contract."""

    schema = InvestigationResult.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "investigation_result",
            "strict": True,
            "schema": json.loads(json.dumps(schema)),
        },
    }


__all__ = [
    "Hypothesis",
    "InvestigationContractError",
    "InvestigationResult",
    "Recommendation",
    "structured_response_format",
    "validate_investigation_result",
]
