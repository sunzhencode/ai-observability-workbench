"""One place that knows which provider implements which channel kind.

`NotificationChannelRevision.provider` and `RouteSnapshot.provider` have existed
since F17, always holding the same default. This is where that column starts to
mean something: the delivery worker will look the implementation up by the kind
recorded on the route snapshot rather than holding one instance (F24 阶段 3).

The registry is also the single gate for FAKE mode. `start.sh --mock` must not
be able to reach the network through *any* kind, so the fake substitution
happens here rather than at each call site, where a new provider could quietly
be added without one.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.config import settings

# Re-exported so callers can depend on this module rather than reaching into the
# Feishu implementation for types that are about to stop being Feishu-specific.
from app.providers.feishu import FeishuConfig, FeishuProvider, ProviderResult
from app.providers.runtime import get_notification_provider
from app.providers.generic_webhook import GenericWebhookProvider
from app.providers.smtp import SmtpProvider

FEISHU_CUSTOM_BOT = "FEISHU_CUSTOM_BOT"
SMTP = "SMTP"
GENERIC_WEBHOOK = "GENERIC_WEBHOOK"

#: Kinds a channel may be created with. A kind appears here only once something
#: can send it, so the API can never offer a channel nothing implements.
SUPPORTED_KINDS: tuple[str, ...] = (FEISHU_CUSTOM_BOT, SMTP, GENERIC_WEBHOOK)

#: Every kind the schema knows about, including ones not yet implemented. Used
#: to read historical rows, never to offer a choice.
KNOWN_KINDS: tuple[str, ...] = (FEISHU_CUSTOM_BOT, SMTP, GENERIC_WEBHOOK)


@runtime_checkable
class NotificationProvider(Protocol):
    """What the delivery worker needs from any channel kind.

    Implementations return a `ProviderResult` instead of raising: a failed send
    is an outcome the Outbox records and retries, not an exception that loses
    the attempt. `transient` is the provider's judgement -- only it knows that
    an SMTP 4xx is worth another try and a 5xx is not.
    """

    kind: str

    async def send(
        self,
        payload: dict[str, Any],
        config: Any,
        *,
        purpose: str | None = None,
    ) -> ProviderResult: ...


class UnsupportedProviderKind(ValueError):
    """A channel names a kind this build cannot send with."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def is_fake_mode() -> bool:
    return settings.notification_provider_mode.strip().upper() == "FAKE"


def get_provider(kind: str) -> NotificationProvider:
    """The implementation for one channel kind.

    In FAKE mode every kind resolves to the same deterministic fake, so a mock
    run cannot perform network I/O whatever a channel says it is. That
    substitution lives here rather than in each provider, because a provider
    added later would be the one to forget it.
    """

    requested = str(kind or "").strip().upper() or FEISHU_CUSTOM_BOT
    if requested not in SUPPORTED_KINDS:
        raise UnsupportedProviderKind(requested)
    if is_fake_mode():
        return get_notification_provider()  # type: ignore[return-value]
    if requested == SMTP:
        return SmtpProvider()
    if requested == GENERIC_WEBHOOK:
        return GenericWebhookProvider()
    return FeishuProvider()


def supported_kinds() -> tuple[str, ...]:
    return SUPPORTED_KINDS


__all__ = [
    "FEISHU_CUSTOM_BOT",
    "SMTP",
    "GENERIC_WEBHOOK",
    "SUPPORTED_KINDS",
    "KNOWN_KINDS",
    "FeishuConfig",
    "FeishuProvider",
    "NotificationProvider",
    "ProviderResult",
    "GenericWebhookProvider",
    "SmtpProvider",
    "UnsupportedProviderKind",
    "get_provider",
    "is_fake_mode",
    "supported_kinds",
]
