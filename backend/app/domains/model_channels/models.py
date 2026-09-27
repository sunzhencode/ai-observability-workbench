"""Pure model-channel lifecycle; credentials are owned by the adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class ModelKind(str, Enum):
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    LOCAL_LOOPBACK = "LOCAL_LOOPBACK"


class ChannelState(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class ModelChannelDraft:
    channel_id: str
    kind: ModelKind
    name: str
    base_url: str
    model: str
    state: ChannelState = ChannelState.DRAFT
    tested_ok: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "channel_id": self.channel_id,
            "kind": self.kind,
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "state": self.state,
            "tested_ok": self.tested_ok,
        }

    def with_update(
        self,
        *,
        kind: ModelKind | None = None,
        name: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> ModelChannelDraft:
        if kind is not None and kind is not self.kind:
            raise ValueError("MODEL_KIND_IMMUTABLE")
        return replace(
            self,
            name=self.name if name is None else name,
            base_url=self.base_url if base_url is None else base_url,
            model=self.model if model is None else model,
            state=ChannelState.DRAFT,
            tested_ok=False,
        )


def activate_revision(draft: ModelChannelDraft) -> ModelChannelDraft:
    if draft.state is not ChannelState.DRAFT:
        raise ValueError("MODEL_CHANNEL_NOT_DRAFT")
    return replace(draft, state=ChannelState.ACTIVE)
