"""Strict API schemas for Web-managed connections and notification channels."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas import UTCDateTime


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def safe_validation_exception_handler(_request, exc: RequestValidationError):
    """Validation failures must not echo secret-bearing request inputs."""
    details: list[dict[str, Any]] = []
    for raw in exc.errors():
        item = {key: value for key, value in raw.items() if key not in {"input", "ctx"}}
        details.append(item)
    return JSONResponse(status_code=422, content={"detail": details})


class SecretUpdate(StrictModel):
    action: Literal["KEEP", "REPLACE", "CLEAR"]
    value: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_replace(self) -> "SecretUpdate":
        if self.action == "REPLACE" and not self.value:
            raise ValueError("REPLACE requires value")
        if self.action != "REPLACE" and self.value is not None:
            raise ValueError("value is accepted only for REPLACE")
        return self


class RevisionActionIn(StrictModel):
    expected_version: int = Field(ge=1)


class TestResultOut(StrictModel):
    ok: bool
    code: str
    transient: bool = False


class OpenAICompatibleModelConfigIn(StrictModel):
    kind: Literal["OPENAI_COMPATIBLE"] = "OPENAI_COMPATIBLE"
    base_url: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretUpdate


class ModelChannelCreateIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    config: OpenAICompatibleModelConfigIn


class ModelChannelDraftUpdate(StrictModel):
    expected_revision_id: int = Field(ge=1)
    config: OpenAICompatibleModelConfigIn


class ModelChannelRevisionOut(StrictModel):
    id: int
    state: str
    base_url: str
    base_host: str
    model: str
    secret_configured: bool
    tested_ok_at: UTCDateTime | None
    created_at: UTCDateTime


class ModelChannelOut(StrictModel):
    id: int
    name: str
    kind: str
    enabled: bool
    active_revision_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    revisions: list[ModelChannelRevisionOut]


class ModelChannelTestOut(StrictModel):
    ok: bool
    code: str
    possibly_billed: bool
    # The safe sub-code (HTTP status, egress reason). Every failure kind maps to
    # a different next action, and without this they all read the same.
    detail: str = ""


class ModelListOut(StrictModel):
    ok: bool
    code: str = "OK"
    detail: str = ""
    models: list[str] = []


class EventSourceEndpointIn(StrictModel):
    # Which slot this endpoint occupies. A position owns a stored credential and
    # is the target of poll-evidence foreign keys, so it cannot be inferred from
    # array order: removing an endpoint shifts every later one, and a `KEEP`
    # secret would then resolve to the previous occupant's. Omit it and the
    # server falls back to array order, which is correct only for a list that
    # has not been reordered.
    position: Optional[int] = Field(default=None, ge=0, le=7)
    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    auth_type: Literal["NONE", "BEARER", "BASIC"] = "NONE"
    username: str = Field(default="", max_length=256)
    secret: SecretUpdate


class EventSourceThanosIn(StrictModel):
    """The optional history address. An empty URL means "do not backfill"."""

    url: str = Field(default="", max_length=2048)
    auth_type: Literal["NONE", "BEARER"] = "NONE"
    username: str = Field(default="", max_length=256)
    secret: SecretUpdate = Field(
        default_factory=lambda: SecretUpdate(action="KEEP")
    )
    timeout_seconds: int = Field(default=15, ge=1, le=120)


class EventSourceGrafanaIn(StrictModel):
    """The optional Grafana address. An empty URL removes the configuration.

    No `auth_type` and no `username`: Grafana here is Bearer-or-anonymous, and
    an internal instance configured for anonymous read is a normal deployment,
    not a misconfigured one. An absent credential *is* the anonymous case.

    **Plain `http` and a private address are both accepted.** Grafana is a
    read-only monitoring source living on the internal network, not an outbound
    target — applying the outbound address rules here would reject the ordinary
    configuration (CAP-13.1a).
    """

    url: str = Field(default="", max_length=2048)
    secret: SecretUpdate = Field(
        default_factory=lambda: SecretUpdate(action="KEEP")
    )
    timeout_seconds: int = Field(default=15, ge=1, le=120)


class EventSourceGrafanaOut(StrictModel):
    url: str
    #: Whether a credential is stored — never the credential itself.
    secret_configured: bool
    timeout_seconds: int
    last_test_status: str | None
    tested_at: UTCDateTime | None
    safe_error_code: str | None


class GrafanaDashboardSummaryOut(StrictModel):
    uid: str
    title: str
    folder_title: str = ""


class GrafanaImportPreviewIn(StrictModel):
    #: The same whitelist the client enforces. Checked twice on purpose: here it
    #: gives the caller a 422 instead of an upstream error, and in the client it
    #: stays a property of the client rather than of one call site.
    dashboard_uid: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ImportSubstitutionOut(StrictModel):
    macro: str
    replacement: str


class ImportPendingVariableOut(StrictModel):
    name: str
    suggestion: Literal["BIND_LABEL", "PIN_VALUE"]
    sample_value: str = ""
    multi: bool = False


class ImportCandidateOut(StrictModel):
    """One panel target, with everything the reader needs to decide about it.

    Both the original and the resolved query are present: approving a
    substitution means being shown what changed, not just the result.
    """

    panel_id: int
    panel_title: str
    ref_id: str
    raw_promql: str
    resolved_promql: str
    status: Literal["READY", "NEEDS_DECISION", "UNSUPPORTED", "UNVERIFIED"]
    suggested_name: str
    order: int
    reason: str = ""
    substitutions: list[ImportSubstitutionOut] = []
    pending_variables: list[ImportPendingVariableOut] = []
    display_unit: str = ""
    probe_value: float | None = None
    probe_note: str = ""
    diff_kind: Literal[
        "NEW", "UNCHANGED", "UPSTREAM_CHANGED", "CONFLICT", "GONE"
    ] = "NEW"
    #: Populated for `UPSTREAM_CHANGED` and `CONFLICT`. A conflict shows three
    #: texts side by side — what was imported, what the user changed it to, and
    #: what Grafana has now — and picks none of them.
    template_id: int | None = None
    imported_promql: str = ""
    current_promql: str = ""


class GrafanaImportPreviewOut(StrictModel):
    dashboard_uid: str
    dashboard_title: str
    candidates: list[ImportCandidateOut]
    #: Whether the live check ran at all. Without a history address every
    #: candidate comes back unverified, and the page has to say why rather than
    #: leaving the reader to infer it.
    probed: bool
    #: For the sentence on the confirmation page: an alert draws at most a fixed
    #: number of auxiliary curves, so "how many are already on" stops being a
    #: non-question the moment forty templates can be added at once.
    enabled_template_count: int
    max_auxiliary_curves: int


class ImportOriginIn(StrictModel):
    dashboard_uid: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    dashboard_title: str = Field(default="", max_length=500)
    panel_id: int = Field(ge=0)
    panel_title: str = Field(default="", max_length=500)
    ref_id: str = Field(default="", max_length=64)


class GrafanaImportItemIn(StrictModel):
    """A ticked candidate as the browser finally submitted it.

    `final_promql` is the literal the user read on screen. The server does not
    re-fetch the dashboard to rebuild it: that would look more careful and be
    less so, because a definition changed between preview and confirmation would
    leave the stored query different from the approved one (review Q4).
    """

    final_promql: str = Field(min_length=1, max_length=4096)
    #: Grafana's own text for this target, before the user's variable bindings.
    #: The re-import diff compares against this, so that binding `$instance` to
    #: `{{instance}}` does not make the candidate look upstream-changed forever.
    #: Empty falls back to `final_promql`.
    imported_promql: str = Field(default="", max_length=4096)
    name: str = Field(min_length=1, max_length=200)
    origin: ImportOriginIn
    required_labels: list[str] = Field(default=[], max_length=16)
    enabled: bool = False
    display_unit: str = Field(default="", max_length=40)
    order: int = Field(default=0, ge=0, le=10_000)
    #: Set only when accepting an upstream change to an already-imported
    #: template.
    template_id: int | None = Field(default=None, ge=1)


class GrafanaImportConfirmIn(StrictModel):
    items: list[GrafanaImportItemIn] = Field(default=[], max_length=200)


class GrafanaImportConfirmOut(StrictModel):
    created_template_ids: list[int]
    updated_template_ids: list[int]


class EventSourceCandidateIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    endpoints: list[EventSourceEndpointIn] = Field(min_length=1, max_length=8)
    thanos: EventSourceThanosIn | None = None
    poll_interval_seconds: int = Field(default=30, ge=5, le=3600)
    resolution_grace_seconds: int = Field(default=60, ge=0, le=86400)
    max_parallel_endpoints: int = Field(default=4, ge=1, le=8)
    watchdog_enabled: bool = False
    watchdog_alertname: str = Field(default="Watchdog", min_length=1, max_length=128)
    watchdog_identity_label: str = Field(default="cluster", min_length=1, max_length=128)
    watchdog_missing_after_seconds: int | None = Field(default=None, ge=60, le=86400)


class EventSourceCreateIn(EventSourceCandidateIn):
    enable: bool = True


class EventSourceUpdateIn(EventSourceCandidateIn):
    expected_version: int = Field(ge=1)


class EventSourceTestIn(StrictModel):
    candidate: EventSourceCandidateIn | None = None
    positions: list[int] | None = Field(default=None, max_length=8)

    @field_validator("positions")
    @classmethod
    def validate_positions(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(item < 0 or item >= 8 for item in value) or len(set(value)) != len(value):
            raise ValueError("positions must contain unique endpoint indexes from 0 to 7")
        return value


class EventSourceEndpointTestOut(StrictModel):
    position: int
    ok: bool
    code: str


class EventSourceTestOut(StrictModel):
    ok: bool
    code: str
    endpoints: list[EventSourceEndpointTestOut]


class EventSourceEndpointLastTestOut(StrictModel):
    ok: bool
    code: str
    tested_at: UTCDateTime


class EventSourceEndpointOut(StrictModel):
    position: int
    url: str
    enabled: bool
    auth_type: str
    username: str
    secret_configured: bool
    last_test: EventSourceEndpointLastTestOut | None


class EventSourceThanosOut(StrictModel):
    url: str
    auth_type: str
    username: str
    secret_configured: bool
    timeout_seconds: int
    last_test_status: str | None
    tested_at: UTCDateTime | None
    safe_error_code: str | None


class EventSourceConfigOut(StrictModel):
    poll_interval_seconds: int
    resolution_grace_seconds: int
    max_parallel_endpoints: int
    watchdog_enabled: bool
    watchdog_alertname: str
    watchdog_identity_label: str
    watchdog_missing_after_seconds: int
    last_test_status: str | None
    tested_at: UTCDateTime | None
    safe_error_code: str | None
    endpoints: list[EventSourceEndpointOut]
    thanos: EventSourceThanosOut | None
    grafana: EventSourceGrafanaOut | None = None


class EventSourceOut(StrictModel):
    id: str
    type: str
    name: str
    lifecycle_state: str
    status: str
    version: int
    config: EventSourceConfigOut | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    archived_at: UTCDateTime | None


class EventSourceAuditOut(StrictModel):
    id: int
    action: str
    actor: str
    result: str
    changes: dict[str, Any]
    created_at: UTCDateTime


class WatchdogClusterCreateIn(StrictModel):
    identity_value: str = Field(min_length=1, max_length=256)


class WatchdogClusterUpdateIn(StrictModel):
    action: Literal["EXPECT", "IGNORE", "RESTORE"]
    reason: str | None = Field(default=None, max_length=512)


class WatchdogClusterOut(StrictModel):
    id: int
    source_id: str
    identity_value: str
    cluster: str
    inventory_state: str
    health_state: str | None
    status: str
    first_discovered_at: UTCDateTime
    monitoring_started_at: UTCDateTime
    last_seen_at: UTCDateTime | None
    ignored_at: UTCDateTime | None
    ignored_reason: str | None
    still_emitting: bool
    freshness_seconds: int | None
    version: int


class WorkbenchUrlIn(StrictModel):
    url: str = Field(default="", max_length=2048)


class WorkbenchUrlOut(StrictModel):
    url: str


class FeishuChannelIn(StrictModel):
    """Feishu custom bot: the webhook carries its own token in the last path
    segment, which is why there is no separate token field."""

    kind: Literal["FEISHU_CUSTOM_BOT"] = "FEISHU_CUSTOM_BOT"
    webhook: SecretUpdate
    signing_secret: SecretUpdate
    required_keyword: str | None = Field(default=None, max_length=64)
    mention_mode: Literal["NONE", "USERS", "ALL"] = "NONE"
    mention_users: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    mention_on: dict[str, bool] = Field(default_factory=dict)


class SmtpChannelIn(StrictModel):
    kind: Literal["SMTP"] = "SMTP"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    tls_mode: Literal["STARTTLS", "TLS"] = "STARTTLS"
    username: str = Field(default="", max_length=255)
    password: SecretUpdate
    from_addr: str = Field(min_length=3, max_length=320)
    to_addrs: list[str] = Field(min_length=1, max_length=50)
    subject_prefix: str = Field(default="", max_length=64)


class GenericWebhookChannelIn(StrictModel):
    kind: Literal["GENERIC_WEBHOOK"] = "GENERIC_WEBHOOK"
    url: str = Field(min_length=8, max_length=2048)
    headers: SecretUpdate
    signing_secret: SecretUpdate
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)


ChannelConfigIn = Annotated[
    FeishuChannelIn | SmtpChannelIn | GenericWebhookChannelIn,
    Field(discriminator="kind"),
]


class ChannelCreateIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    config: ChannelConfigIn


class ChannelDraftUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    config: ChannelConfigIn


class ChannelConfigSummaryOut(StrictModel):
    kind: str
    target: str
    detail: str
    secret_configured: bool


class ChannelRevisionOut(StrictModel):
    id: int
    version: int
    state: str
    provider: str
    config_summary: ChannelConfigSummaryOut
    webhook_configured: bool
    signing_secret_configured: bool
    required_keyword: str | None
    mention_mode: str
    mention_users: list[dict[str, str]]
    mention_on: dict[str, bool]
    last_tested_at: UTCDateTime | None
    last_test_status: str | None
    last_test_error_code: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    activated_at: UTCDateTime | None


class ChannelOut(StrictModel):
    id: int
    name: str
    state: str
    active_revision_id: int | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    disabled_at: UTCDateTime | None
    revisions: list[ChannelRevisionOut]


class PolicyMatcherIn(StrictModel):
    field: str = Field(min_length=1, max_length=128)
    operator: Literal["=", "!=", "=~", "!~"]
    value: str = Field(max_length=512)


class SourceScopeIn(StrictModel):
    mode: Literal["ALL", "SELECTED"] = "ALL"
    source_ids: list[str] = Field(default_factory=list, max_length=100)


class PolicyDraftIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(default=100, ge=-1_000_000, le=1_000_000)
    matchers: list[PolicyMatcherIn] = Field(default_factory=list, max_length=20)
    repeat_interval_seconds: int = Field(default=14_400, ge=0, le=2_592_000)
    channel_ids: list[int] = Field(min_length=1, max_length=20)
    source_scope: SourceScopeIn = Field(default_factory=SourceScopeIn)


class PolicyDraftUpdate(PolicyDraftIn):
    expected_version: int = Field(ge=1)


class PolicyPreviewIn(PolicyDraftIn):
    logical_id: str | None = None


class PolicyOut(StrictModel):
    id: int
    logical_id: str
    version: int
    name: str
    state: str
    priority: int
    matchers: list[dict[str, str]]
    repeat_interval_seconds: int
    channel_ids: list[int]
    source_scope: dict[str, Any]
    created_at: UTCDateTime
    updated_at: UTCDateTime
    activated_at: UTCDateTime | None


class PolicyPreviewSampleOut(StrictModel):
    incident_id: int
    context: dict[str, Any]
    winner_policy_id: int | None
    winner_policy_name: str | None
    channel_ids: list[int]


class PolicyPreviewOut(StrictModel):
    direct_match_count: int
    shadowed_count: int
    final_match_count: int
    firing_match_count: int
    unrouted_count: int
    disabled_channel_count: int
    samples: list[PolicyPreviewSampleOut]
    example_card: dict[str, Any]


class PrepareActivationIn(RevisionActionIn):
    notify_existing: bool = False


class PrepareActivationOut(StrictModel):
    confirm_token: str
    eligible_incident_count: int
    expires_at_epoch: int


class ConfirmActivationIn(PrepareActivationIn):
    confirm_token: str = Field(min_length=20, max_length=4096)


class DeliveryOut(StrictModel):
    id: int
    event_key: str
    incident_id: int
    route_id: int
    route_target_id: int
    channel_id: int
    channel_name: str
    source_id: str
    source_name: str | None
    event_type: str
    state: str
    attempt_count: int
    scheduled_at: UTCDateTime
    next_attempt_at: UTCDateTime
    suppression_reason: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    succeeded_at: UTCDateTime | None


class AttemptOut(StrictModel):
    id: int
    attempt_no: int
    trigger: str
    started_at: UTCDateTime
    finished_at: UTCDateTime | None
    outcome: str | None
    http_status: int | None
    provider_request_id: str | None
    error_code: str | None
    error_summary: str | None


class DeliveryDetailOut(DeliveryOut):
    payload_snapshot: dict[str, Any]
    attempts: list[AttemptOut]


class ManualRetryOut(StrictModel):
    delivery: DeliveryOut
    warning: str


class IncidentNotificationOut(StrictModel):
    incident_id: int
    occurrence_no: int
    route: dict[str, Any] | None
    targets: list[dict[str, Any]]
    deliveries: list[DeliveryOut]
