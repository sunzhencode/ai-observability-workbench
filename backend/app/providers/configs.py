"""What each channel kind needs in order to send.

Before F24 these fields were columns on `NotificationChannelRevision`, which
worked while there was one kind. Three kinds cannot share one set of columns:
SMTP needs a host, a port, an account and recipients; a generic webhook needs a
URL and headers; neither has any notion of "@ whom". So the shape moves into a
per-kind model stored in `config_envelope`.

Secrets live inside these models as **encrypted envelopes** (the dicts produced
by `crypto.SecretBox`), never as plaintext. `resolved()` is the only place that
opens them, and it returns the provider's own config object -- so a config model
can be logged or serialised without leaking, and only the send path decrypts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.egress import EgressRejected, assert_https_shape, assert_public_smtp
from app.providers.feishu import FeishuConfig, validate_feishu_webhook

Envelope = dict[str, Any]


class ProviderConfigModel(BaseModel):
    """Base for every kind's stored configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: str

    def secret_envelopes(self) -> list[Envelope]:
        """Every envelope this config holds, for re-encryption and audit checks."""

        return []


class FeishuChannelConfig(ProviderConfigModel):
    kind: Literal["FEISHU_CUSTOM_BOT"] = "FEISHU_CUSTOM_BOT"
    webhook: Envelope = Field(default_factory=dict)
    signing_secret: Envelope | None = None
    required_keyword: str | None = Field(default=None, max_length=64)
    mention_mode: Literal["NONE", "USERS", "ALL"] = "NONE"
    mention_users: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    mention_on: dict[str, bool] = Field(default_factory=dict)

    def secret_envelopes(self) -> list[Envelope]:
        return [item for item in (self.webhook, self.signing_secret) if item]


class SmtpChannelConfig(ProviderConfigModel):
    kind: Literal["SMTP"] = "SMTP"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    tls_mode: Literal["STARTTLS", "TLS"] = "STARTTLS"
    username: str = Field(default="", max_length=255)
    password: Envelope | None = None
    from_addr: str = Field(min_length=3, max_length=320)
    to_addrs: list[str] = Field(min_length=1, max_length=50)
    subject_prefix: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_target(self) -> "SmtpChannelConfig":
        # Port allowlist is checked here so a bad port is a save-time error with
        # a readable message, not a send-time failure. The address check runs
        # again before every send, because DNS changes.
        if self.port not in (465, 587):
            raise ValueError("SMTP port must be 587 (STARTTLS) or 465 (implicit TLS)")
        if self.port == 465 and self.tls_mode != "TLS":
            raise ValueError("port 465 requires implicit TLS")
        if self.port == 587 and self.tls_mode != "STARTTLS":
            raise ValueError("port 587 requires STARTTLS")
        for address in [self.from_addr, *self.to_addrs]:
            if "@" not in address or address.strip() != address:
                raise ValueError(f"not a usable mail address: {address}")
        return self

    def secret_envelopes(self) -> list[Envelope]:
        return [self.password] if self.password else []


class GenericWebhookChannelConfig(ProviderConfigModel):
    kind: Literal["GENERIC_WEBHOOK"] = "GENERIC_WEBHOOK"
    url: str = Field(min_length=8, max_length=2048)
    # Headers are treated wholesale as a secret: a token in a header is the
    # commonest way to authenticate a webhook, and echoing it back would leak it.
    headers: Envelope | None = None
    signing_secret: Envelope | None = None
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    @model_validator(mode="after")
    def validate_target(self) -> "GenericWebhookChannelConfig":
        try:
            # Shape only: no DNS at save time. The resolved-address check runs
            # in the provider before every send, because records change and
            # because a save should not fail on a lookup.
            assert_https_shape(self.url)
        except EgressRejected as exc:
            raise ValueError(f"webhook target refused: {exc.code}") from exc
        return self

    def secret_envelopes(self) -> list[Envelope]:
        return [item for item in (self.headers, self.signing_secret) if item]


CONFIG_MODELS: dict[str, type[ProviderConfigModel]] = {
    "FEISHU_CUSTOM_BOT": FeishuChannelConfig,
    "SMTP": SmtpChannelConfig,
    "GENERIC_WEBHOOK": GenericWebhookChannelConfig,
}


def config_model_for(kind: str) -> type[ProviderConfigModel]:
    try:
        return CONFIG_MODELS[str(kind or "").strip().upper() or "FEISHU_CUSTOM_BOT"]
    except KeyError as exc:
        raise ValueError(f"unknown channel kind: {kind}") from exc


def parse_channel_config(kind: str, payload: dict[str, Any]) -> ProviderConfigModel:
    """Validate a stored or submitted config blob against its kind."""

    model = config_model_for(kind)
    data = dict(payload or {})
    data.setdefault("kind", model.model_fields["kind"].default)
    return model.model_validate(data)


def resolve_feishu(config: FeishuChannelConfig, decrypt) -> FeishuConfig:
    """Open the envelopes and produce what `FeishuProvider.send` expects."""

    webhook = decrypt(config.webhook)
    signing_secret = decrypt(config.signing_secret) if config.signing_secret else ""
    return FeishuConfig(
        webhook=validate_feishu_webhook(webhook),
        signing_secret=signing_secret,
        required_keyword=config.required_keyword,
    )


__all__ = [
    "CONFIG_MODELS",
    "FeishuChannelConfig",
    "GenericWebhookChannelConfig",
    "ProviderConfigModel",
    "SmtpChannelConfig",
    "config_model_for",
    "parse_channel_config",
    "resolve_feishu",
    "assert_public_smtp",
]
