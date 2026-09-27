"""What every model client must look like, and what it may report back.

Deliberately small. ADR 0010 refuses a provider abstraction layer until there is
a second implementation to validate it against (HISTORY §3.10: a reserved field
is worth nothing until something real is pushed through it), so this is a
protocol and two result types -- not a framework.

The one thing worth designing carefully here is failure reporting, because two
decisions in the SDD hang off it:

- **A billed request is never retried automatically** (D45). So the result has
  to say whether the request *reached* the service, not just whether it
  succeeded. A timeout after the model started generating has been paid for.
- **Errors must be safe to surface** (D51). `str(exc)` on an httpx error carries
  the full URL; the codes here are fixed strings and the caller decides how
  much to say.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


class ModelFailureKind(str, Enum):
    """Why a call did not produce a usable answer. Each maps to a distinct action."""

    EGRESS_REJECTED = "EGRESS_REJECTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNREACHABLE = "UNREACHABLE"
    TIMEOUT = "TIMEOUT"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_ERROR = "SERVICE_ERROR"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"


#: Failures where the request provably never reached the service, so no billing
#: could have occurred. **Everything not in here must be treated as possibly
#: billed** (D45) -- including timeouts, which are the tempting ones to retry.
NEVER_BILLED: frozenset[ModelFailureKind] = frozenset(
    {
        ModelFailureKind.EGRESS_REJECTED,
        ModelFailureKind.NOT_CONFIGURED,
        ModelFailureKind.UNREACHABLE,
    }
)


class ModelCallError(RuntimeError):
    """A failed model call. `kind` is safe to surface; the address is not."""

    def __init__(self, kind: ModelFailureKind, detail: str = "") -> None:
        super().__init__(kind.value)
        self.kind = kind
        # A short, already-safe code (an HTTP status, an exception class name).
        # Never a URL, never a response body, never the user's prompt.
        self.detail = detail

    @property
    def possibly_billed(self) -> bool:
        return self.kind not in NEVER_BILLED


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for. Only ever the four read-only ones."""

    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ModelReply:
    """One turn back from the service.

    Either it asked for tools or it answered; both being empty is a malformed
    reply, which the client raises on rather than passing along as "nothing".
    """

    text: str = ""
    tool_calls: Sequence[ToolCall] = field(default_factory=tuple)
    # Reported by the service, recorded for the audit trail. Never trusted for
    # anything but display -- the model does not get to tell us what it cost.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_name: str = ""


@runtime_checkable
class ModelClient(Protocol):
    """The whole contract. One method, because one call is the whole design."""

    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        response_format: Mapping[str, Any] | None = None,
    ) -> ModelReply:
        """Send one turn. Raises `ModelCallError`; never retries internally.

        Retry policy belongs to the caller because only the caller knows whether
        the request could have been billed (D45), and a client that retries on
        its own makes that decision invisible.
        """
        ...
