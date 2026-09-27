"""At-most-once command execution over durable, secret-free receipts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any, Protocol


class CommandConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExternalOutcomeUnknown(RuntimeError):
    """The remote side may have accepted the request before transport failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CommandClaim:
    scope: str
    key_hash: str
    request_hash: str
    replay: Mapping[str, Any] | None = None


class CommandReceiptPort(Protocol):
    def claim(self, scope: str, key: str, payload: str) -> CommandClaim: ...
    def complete(self, claim: CommandClaim, response: Mapping[str, Any]) -> None: ...
    def release(self, claim: CommandClaim) -> None: ...


class IdempotentCommands:
    """Execute DB-only create commands once and replay their safe response."""

    def __init__(self, receipts: CommandReceiptPort) -> None:
        self._receipts = receipts

    def execute(
        self,
        *,
        scope: str,
        key: str,
        payload: Mapping[str, Any],
        action: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        claim = self._receipts.claim(scope, key, serialized)
        if claim.replay is not None:
            return claim.replay
        try:
            response = action()
        except Exception:
            # These guarded commands are DB-only creates. Their adapters either
            # commit the resource or raise; releasing a failed validation/store
            # attempt lets the operator correct and retry the same interaction.
            self._receipts.release(claim)
            raise
        self._receipts.complete(claim, response)
        return response

    async def execute_external(
        self,
        *,
        scope: str,
        key: str,
        payload: Mapping[str, Any],
        action: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        """Run an external-effect command once; ambiguous failures stay blocked."""
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        claim = self._receipts.claim(scope, key, serialized)
        if claim.replay is not None:
            return claim.replay
        # Do not release the claim on exception: the provider may have accepted
        # the request before the connection failed. Reuse must not send again.
        response = await action()
        self._receipts.complete(claim, response)
        return response
