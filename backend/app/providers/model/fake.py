"""A model client that never opens a socket.

Two jobs, and the second is the one that matters:

1.  deterministic replies for tests, so the tool loop and the structure
    validator can be exercised without a paid call;
2.  **the thing `start.sh --mock` substitutes in**, so a mock run cannot reach a
    real model service even if a channel is configured with a real key.

It holds no transport at all rather than a stubbed one -- there is no field to
misconfigure into reaching the network.

The script mirrors the notification domain's `HTTP_500,OK` idiom, with one
deliberate difference in meaning: here a scripted failure must be observed to
*stop* the caller, not to make it try again. Nothing in this repository may
retry a possibly-billed call (D45).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .base import ModelCallError, ModelFailureKind, ModelReply, ToolCall

#: `OK` yields a canned reply; anything else is treated as a failure kind name,
#: falling back to SERVICE_ERROR so a typo fails closed rather than passing.
SCRIPT_OK = "OK"


@dataclass(frozen=True)
class RecordedCall:
    """One turn this client was asked for.

    A record type rather than a dict so a test asserting on the wrong key fails
    at the attribute rather than silently comparing against `None`.
    """

    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    response_format: Mapping[str, Any]


@dataclass
class FakeModelClient:
    """Replies from a fixed script. Records what it was asked, sends nothing."""

    replies: list[ModelReply | ModelCallError] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)
    #: What to answer once the script runs out. A model that keeps asking for
    #: tools forever is a real failure mode, so the default is a final answer
    #: rather than an endless supply of tool calls.
    default: ModelReply | None = None

    @classmethod
    def from_script(cls, script: str) -> "FakeModelClient":
        """`OK` / `HTTP_500,OK` -> a queue of replies and failures."""
        replies: list[ModelReply | ModelCallError] = []
        for token in (part.strip() for part in str(script or "").split(",")):
            if not token:
                continue
            if token.upper() == SCRIPT_OK:
                replies.append(_canned_reply())
                continue
            kind = _failure_kind(token)
            replies.append(ModelCallError(kind, token.upper()))
        return cls(replies=replies)

    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        response_format: Mapping[str, Any] | None = None,
    ) -> ModelReply:
        self.calls.append(
            RecordedCall(
                messages=tuple(dict(item) for item in messages),
                tools=tuple(dict(item) for item in tools),
                response_format=dict(response_format or {}),
            )
        )
        if self.replies:
            item = self.replies.pop(0)
            if isinstance(item, ModelCallError):
                # Raised, never swallowed: the caller has to decide, because
                # only the caller knows whether this one may have been billed.
                raise item
            if _is_planner_call(tools) and item.text == _canned_reply().text:
                return _planner_finish_reply()
            return item
        if self.default is not None:
            return self.default
        return _planner_finish_reply() if _is_planner_call(tools) else _canned_reply()

    def queue_tool_call(self, name: str, /, **arguments: Any) -> "FakeModelClient":
        self.replies.append(
            ModelReply(
                tool_calls=(
                    ToolCall(call_id=f"call_{len(self.replies)}", name=name, arguments=arguments),
                ),
                model_name="fake-model",
            )
        )
        return self

    def queue_text(self, text: str) -> "FakeModelClient":
        self.replies.append(ModelReply(text=text, model_name="fake-model"))
        return self


def _failure_kind(token: str) -> ModelFailureKind:
    upper = token.upper()
    if upper.startswith("HTTP_"):
        code = upper.removeprefix("HTTP_")
        if code in {"401", "403"}:
            return ModelFailureKind.AUTH_FAILED
        if code == "429":
            return ModelFailureKind.RATE_LIMITED
        return ModelFailureKind.SERVICE_ERROR
    try:
        return ModelFailureKind(upper)
    except ValueError:
        # An unrecognised token is a scripting mistake. Failing closed as a
        # service error beats silently behaving like OK.
        return ModelFailureKind.SERVICE_ERROR


#: The synthetic investigation answer a channel "test" must produce to pass.
#:
#: Spelled out here rather than imported, because `providers/` must not depend on
#: `services/` (CLAUDE.md's layering). The duplication is deliberate but **not
#: unguarded**: `test_model_channels.py` runs this literal through the real
#: validator, so if the contract changes and this does not, a test goes red
#: rather than mock mode quietly failing every channel test.
_SYNTHETIC_CONTRACT_REPLY = {
    "hypotheses": [
        {
            "statement": "The synthetic metric fact supports the synthetic alert.",
            "verdict": "SUPPORTED",
            "supporting_fact_ids": ["synthetic_metric_fact_001"],
            "contradicting_fact_ids": [],
            "missing_evidence": [],
            "recommendations": [],
        }
    ]
}


def _canned_reply() -> ModelReply:
    """What `OK` means.

    The investigation shape rather than the authoring one: `OK` exists so that
    `start.sh --mock` can walk the channel-test flow end to end, and that flow
    validates against the investigation contract. The authoring tests queue
    their own replies, so nothing there depends on this choice.
    """
    return ModelReply(
        text=json.dumps(_SYNTHETIC_CONTRACT_REPLY, ensure_ascii=False),
        model_name="fake-model",
    )


def _is_planner_call(tools: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(item.get("function"), dict)
        and item["function"].get("name") == "ListMetrics"
        for item in tools
    )


def _planner_finish_reply() -> ModelReply:
    return ModelReply(
        text='{"action":"FINISH"}',
        model_name="fake-model",
    )


__all__ = ["FakeModelClient", "RecordedCall", "SCRIPT_OK"]
