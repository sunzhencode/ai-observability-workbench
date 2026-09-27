"""Versioned operator guidance that cannot alter the investigation safety kernel."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json

BUILTIN_PROFILE_ID = "builtin-standard"
BUILTIN_PROFILE_REVISION = 1
MAX_GUIDANCE_CHARS = 8_000
EGRESS_CATEGORIES = (
    "告警内容（可能含主机名、namespace、集群名）",
    "指标摘要与有界采样",
    "人工 Note 与相似历史处置",
)
IMMUTABLE_LAYERS = (
    "字段可见性",
    "只读工具 schema",
    "PromQL 编译器",
    "证据引用 allow-set",
    "结构化输出 schema",
    "调用与查询预算",
    "模型无写边界",
)


@dataclass(frozen=True, slots=True)
class PromptGuidanceV1:
    organization_context: str = ""
    investigation_focus: str = ""
    terminology: str = ""
    response_style: str = ""

    def __post_init__(self) -> None:
        values = asdict(self)
        if any(len(value) > 2_000 for value in values.values()):
            raise ValueError("PROMPT_PROFILE_FIELD_TOO_LONG")
        if sum(len(value) for value in values.values()) > MAX_GUIDANCE_CHARS:
            raise ValueError("PROMPT_PROFILE_TOO_LONG")

    def compact(self) -> dict[str, str]:
        return {name: value.strip() for name, value in asdict(self).items() if value.strip()}


@dataclass(frozen=True, slots=True)
class PromptProfileView:
    id: str
    name: str
    builtin: bool
    status: str
    revision: int
    active_revision: int | None
    guidance: PromptGuidanceV1
    is_global_default: bool
    service_ids: tuple[int, ...]
    tested_at: datetime | None = None
    last_test_code: str | None = None


@dataclass(frozen=True, slots=True)
class PromptProfileRevisionView:
    revision: int
    status: str
    guidance: PromptGuidanceV1
    tested_at: datetime | None
    last_test_code: str | None
    created_at: datetime | None
    changed_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromptProfilePreview:
    profile_id: str
    revision: int
    safety_kernel: tuple[str, ...]
    operator_guidance: dict[str, str]
    egress_categories: tuple[str, ...]
    estimated_tokens: int
    maximum_cost: str


def builtin_profile(*, is_global_default: bool = True) -> PromptProfileView:
    return PromptProfileView(
        id=BUILTIN_PROFILE_ID,
        name="内置标准",
        builtin=True,
        status="ACTIVE",
        revision=BUILTIN_PROFILE_REVISION,
        active_revision=BUILTIN_PROFILE_REVISION,
        guidance=PromptGuidanceV1(),
        is_global_default=is_global_default,
        service_ids=(),
        tested_at=None,
        last_test_code="BUILTIN_CONTRACT_OK",
    )


def preview_profile(profile: PromptProfileView) -> PromptProfilePreview:
    guidance = profile.guidance.compact()
    serialized = json.dumps(guidance, ensure_ascii=False, sort_keys=True)
    return PromptProfilePreview(
        profile_id=profile.id,
        revision=profile.revision,
        safety_kernel=IMMUTABLE_LAYERS,
        operator_guidance=guidance,
        egress_categories=EGRESS_CATEGORIES,
        estimated_tokens=max(1, len(serialized.encode("utf-8"))),
        maximum_cost="UNKNOWN",
    )


def profile_contract_code(profile: PromptProfileView) -> str:
    """Local-only validation. It never calls a model or changes activation."""
    preview = preview_profile(profile)
    if tuple(preview.safety_kernel) != IMMUTABLE_LAYERS:
        return "PROMPT_SAFETY_KERNEL_INVALID"
    if preview.estimated_tokens > MAX_GUIDANCE_CHARS * 4:
        return "PROMPT_PROFILE_TOKEN_ESTIMATE_EXCEEDED"
    return "OK"


__all__ = [
    "BUILTIN_PROFILE_ID",
    "BUILTIN_PROFILE_REVISION",
    "EGRESS_CATEGORIES",
    "IMMUTABLE_LAYERS",
    "PromptGuidanceV1",
    "PromptProfilePreview",
    "PromptProfileRevisionView",
    "PromptProfileView",
    "builtin_profile",
    "preview_profile",
    "profile_contract_code",
]
