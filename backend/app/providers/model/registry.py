"""Which implementation serves which model kind, and the single FAKE gate.

Same shape and the same reason as `app/providers/registry.py`: the mock
substitution happens **here** rather than at each call site, so a new kind
cannot be added without one. A call site that builds its own client is a way to
reach the network from a `--mock` run, and that is precisely what mock mode
promises cannot happen.

Fails closed on an unrecognised process mode. "Neither REAL nor FAKE" must not
resolve to REAL by accident -- that would send a real request during a mock run.
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.providers.egress import Resolver

from .base import ModelClient
from .egress import MODEL_KIND_OPENAI_COMPATIBLE
from .fake import FakeModelClient
from .openai_compatible import OpenAICompatibleClient, OpenAICompatibleConfig
from .openai_responses import OpenAIResponsesClient, uses_responses_api

#: Kinds a channel may be created with. A kind appears here only once something
#: can actually send it, so the API can never offer one nothing implements.
SUPPORTED_MODEL_KINDS: tuple[str, ...] = (MODEL_KIND_OPENAI_COMPATIBLE,)


class UnsupportedModelKind(ValueError):
    """A kind with no implementation. Never assumed to be the default one."""


class UnsupportedModelMode(RuntimeError):
    """The process switch is neither REAL nor FAKE; refuse rather than guess."""


def is_fake_mode() -> bool:
    mode = str(getattr(settings, "model_provider_mode", "REAL") or "").upper()
    if mode not in {"REAL", "FAKE"}:
        raise UnsupportedModelMode("model_provider_mode must be REAL or FAKE")
    return mode == "FAKE"


def supported_kinds() -> tuple[str, ...]:
    return SUPPORTED_MODEL_KINDS


def get_model_client(
    kind: str,
    config: OpenAICompatibleConfig | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> ModelClient:
    """The only place a model client is constructed.

    Takes an already-validated config object rather than loose strings, so a
    caller cannot assemble a half-built target here; validation belongs to the
    config type and happens once.
    """

    if kind not in SUPPORTED_MODEL_KINDS:
        raise UnsupportedModelKind(f"unsupported model kind: {kind!r}")
    if is_fake_mode():
        # After the kind check so a mock run still rejects a bad kind, and
        # before config use so mock mode never needs a real base_url.
        return FakeModelClient.from_script(
            str(getattr(settings, "model_fake_script", "OK") or "OK")
        )
    if config is None:
        raise UnsupportedModelKind("a real model client needs a resolved config")
    if uses_responses_api(config.base_url):
        return OpenAIResponsesClient(config, transport=transport, resolver=resolver)
    return OpenAICompatibleClient(config, transport=transport, resolver=resolver)


__all__ = [
    "SUPPORTED_MODEL_KINDS",
    "UnsupportedModelKind",
    "UnsupportedModelMode",
    "get_model_client",
    "is_fake_mode",
    "supported_kinds",
]
