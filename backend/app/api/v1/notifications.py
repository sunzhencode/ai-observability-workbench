"""Notification channel, policy and delivery HTTP surface."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, status

from app.api.v1.schemas import (
    ErrorEnvelope,
    NotificationAttemptResponse,
    NotificationChannelActionInput,
    NotificationChannelInput,
    NotificationChannelResponse,
    NotificationChannelUpdateInput,
    NotificationDeliveryResponse,
    IncidentNotificationResponse,
    NotificationMatcherInput,
    NotificationPolicyActivationInput,
    NotificationPolicyActivationPrepareInput,
    NotificationPolicyActivationPrepareResponse,
    NotificationPolicyActionInput,
    NotificationPolicyInput,
    NotificationPolicyPreviewResponse,
    NotificationPolicyPreviewSampleResponse,
    NotificationPolicyResponse,
    NotificationPolicyUpdateInput,
    NotificationRetryResponse,
    NotificationRouteResponse,
    NotificationRouteTargetResponse,
    WorkbenchUrlInput,
    WorkbenchUrlResponse,
)
from app.application.notifications import (
    ActivationTokenError,
    ActivationTokenService,
    ChannelDraft,
    ChannelView,
    ConfirmPolicyActivation,
    DeliveryView,
    NotificationProviderFactory,
    NotificationStore,
    PolicyDraft,
    PolicyView,
    PreparePolicyActivation,
    SecretChange,
    TestNotificationChannel,
)
from app.application.commands import IdempotentCommands
from app.api.v1.idempotency import execute_idempotent_create, execute_idempotent_external
from app.domains.notifications.models import (
    Matcher,
    MatcherOperator,
    PolicyCandidate,
    validate_channel_configuration,
)
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def _errors() -> dict[int | str, dict[str, Any]]:
    return {
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    }


def _safe(exc: Exception) -> SafeApiError:
    if isinstance(exc, SafeApiError):
        return exc
    if isinstance(exc, LookupError):
        return SafeApiError(status_code=404, code="NOTIFICATION_RESOURCE_NOT_FOUND", message="未找到指定通知对象")
    if isinstance(exc, (FileExistsError, ActivationTokenError)):
        return SafeApiError(status_code=409, code=str(exc)[:96], message="通知对象已变更或确认凭据已失效，请重新检查影响")
    if isinstance(exc, RuntimeError):
        return SafeApiError(status_code=409, code=str(exc)[:96], message="通知对象当前状态不允许执行此命令")
    return SafeApiError(status_code=422, code="NOTIFICATION_CONFIGURATION_INVALID", message="通知配置字段、范围或安全要求不符合约束")


def _channel(value: ChannelView) -> NotificationChannelResponse:
    return NotificationChannelResponse(
        id=value.id,
        name=value.name,
        provider=value.provider,
        enabled=value.enabled,
        revision_no=value.revision_no,
        revision_state=value.revision_state,
        config=dict(value.config),
        secret_configured=dict(value.secret_configured),
        tested_at=None if value.tested_at is None else to_utc_iso(value.tested_at),
        last_test_code=value.last_test_code,
    )


def _matchers(values: list[NotificationMatcherInput]) -> tuple[Matcher, ...]:
    return tuple(Matcher(item.field, MatcherOperator(item.operator), item.value) for item in values)


def _policy_draft(value: NotificationPolicyInput) -> PolicyDraft:
    return PolicyDraft(
        value.name,
        value.priority,
        _matchers(value.matchers),
        value.repeat_interval_seconds,
        tuple(value.channel_ids),
        value.scope_mode,
        tuple(value.source_ids),
    )


def _policy(value: PolicyView) -> NotificationPolicyResponse:
    return NotificationPolicyResponse(
        revision_id=value.revision_id,
        logical_id=value.logical_id,
        version=value.version,
        name=value.name,
        state=value.state,
        priority=value.priority,
        matchers=[
            NotificationMatcherInput(field=item.field, operator=item.operator.value, value=item.value)
            for item in value.matchers
        ],
        repeat_interval_seconds=value.repeat_interval_seconds,
        channel_ids=list(value.channel_ids),
        scope_mode=value.scope_mode,
        source_ids=list(value.source_ids),
    )


def _delivery(value: DeliveryView) -> NotificationDeliveryResponse:
    return NotificationDeliveryResponse(
        id=value.id,
        event_key=value.event_key,
        incident_id=value.incident_id,
        occurrence_no=value.occurrence_no,
        route_id=value.route_id,
        route_target_id=value.route_target_id,
        channel_id=value.channel_id,
        channel_name=value.channel_name,
        provider=value.provider,
        event_type=value.event_type,
        state=value.state,
        attempt_count=value.attempt_count,
        scheduled_at=to_utc_iso(value.scheduled_at),
        next_attempt_at=to_utc_iso(value.next_attempt_at),
        suppression_reason=value.suppression_reason,
        payload_snapshot=dict(value.payload_snapshot),
        succeeded_at=None if value.succeeded_at is None else to_utc_iso(value.succeeded_at),
        attempts=[
            NotificationAttemptResponse(
                id=item.id,
                attempt_no=item.attempt_no,
                trigger=item.trigger,
                started_at=to_utc_iso(item.started_at),
                finished_at=None if item.finished_at is None else to_utc_iso(item.finished_at),
                outcome=item.outcome,
                http_status=item.http_status,
                provider_request_id=item.provider_request_id,
                error_code=item.error_code,
            )
            for item in value.attempts
        ],
    )


def _channel_draft(value: NotificationChannelInput) -> ChannelDraft:
    secrets = {key: SecretChange(item.action, item.value) for key, item in value.secrets.items()}
    config = validate_channel_configuration(value.provider, value.config)
    return ChannelDraft(value.name, value.provider, config, secrets)


def create_notifications_router(
    *,
    store: NotificationStore,
    providers: NotificationProviderFactory,
    activation_tokens: ActivationTokenService,
    commands: IdempotentCommands,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    errors = _errors()

    @router.get(
        "/settings/workbench-url",
        response_model=WorkbenchUrlResponse,
        responses=errors,
    )
    async def get_workbench_url() -> WorkbenchUrlResponse:
        return WorkbenchUrlResponse(url=store.get_workbench_url())

    @router.put(
        "/settings/workbench-url",
        response_model=WorkbenchUrlResponse,
        responses=errors,
    )
    async def save_workbench_url(payload: WorkbenchUrlInput) -> WorkbenchUrlResponse:
        try:
            value = store.save_workbench_url(payload.url, now=datetime.now(timezone.utc))
        except ValueError as exc:
            raise SafeApiError(
                status_code=422,
                code="WORKBENCH_URL_INVALID",
                message="工作台地址必须为空或使用 http/https",
            ) from exc
        return WorkbenchUrlResponse(url=value)

    @router.get("/notification-channels", response_model=list[NotificationChannelResponse], responses=errors)
    async def list_channels() -> list[NotificationChannelResponse]:
        return [_channel(item) for item in store.list_channels()]

    @router.post("/notification-channels", response_model=NotificationChannelResponse, status_code=status.HTTP_201_CREATED, responses=errors)
    async def create_channel(
        payload: NotificationChannelInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> NotificationChannelResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope="notification-channel.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _channel(
                    store.create_channel(_channel_draft(payload), now=datetime.now(timezone.utc))
                ).model_dump(mode="json"),
            )
            return NotificationChannelResponse.model_validate(rendered)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.put("/notification-channels/{channel_id}/draft", response_model=NotificationChannelResponse, responses=errors)
    async def update_channel(channel_id: str, payload: NotificationChannelUpdateInput) -> NotificationChannelResponse:
        try:
            return _channel(store.update_channel(channel_id, _channel_draft(payload), expected_revision=payload.expected_revision, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-channels/{channel_id}/test", response_model=NotificationChannelResponse, responses=errors)
    async def test_channel(
        channel_id: str,
        payload: NotificationChannelActionInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> NotificationChannelResponse:
        async def action() -> dict[str, Any]:
            value = await TestNotificationChannel(store, providers).execute(
                channel_id,
                expected_revision=payload.expected_revision,
                now=datetime.now(timezone.utc),
            )
            return _channel(value).model_dump(mode="json")

        try:
            rendered = await execute_idempotent_external(
                commands,
                scope="notification-channel.test",
                key=idempotency_key,
                payload={"channel_id": channel_id, **payload.model_dump(mode="json")},
                action=action,
            )
            return NotificationChannelResponse.model_validate(rendered)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-channels/{channel_id}/activate", response_model=NotificationChannelResponse, responses=errors)
    async def activate_channel(channel_id: str, payload: NotificationChannelActionInput) -> NotificationChannelResponse:
        try:
            return _channel(store.activate_channel(channel_id, expected_revision=payload.expected_revision, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-channels/{channel_id}/enable", response_model=NotificationChannelResponse, responses=errors)
    async def enable_channel(channel_id: str, payload: NotificationChannelActionInput) -> NotificationChannelResponse:
        try:
            return _channel(store.set_channel_enabled(channel_id, expected_revision=payload.expected_revision, enabled=True, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-channels/{channel_id}/disable", response_model=NotificationChannelResponse, responses=errors)
    async def disable_channel(channel_id: str, payload: NotificationChannelActionInput) -> NotificationChannelResponse:
        try:
            return _channel(store.set_channel_enabled(channel_id, expected_revision=payload.expected_revision, enabled=False, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.get("/notification-policies", response_model=list[NotificationPolicyResponse], responses=errors)
    async def list_policies() -> list[NotificationPolicyResponse]:
        return [_policy(item) for item in store.list_policies()]

    @router.post("/notification-policies", response_model=NotificationPolicyResponse, status_code=status.HTTP_201_CREATED, responses=errors)
    async def create_policy(
        payload: NotificationPolicyInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> NotificationPolicyResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope="notification-policy.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _policy(
                    store.create_policy(_policy_draft(payload), now=datetime.now(timezone.utc))
                ).model_dump(mode="json"),
            )
            return NotificationPolicyResponse.model_validate(rendered)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.put("/notification-policies/{logical_id}/draft", response_model=NotificationPolicyResponse, responses=errors)
    async def update_policy(logical_id: str, payload: NotificationPolicyUpdateInput) -> NotificationPolicyResponse:
        try:
            return _policy(store.update_policy(logical_id, _policy_draft(payload), expected_version=payload.expected_version, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-policies/preview", response_model=NotificationPolicyPreviewResponse, responses=errors)
    async def preview_policy(payload: NotificationPolicyInput) -> NotificationPolicyPreviewResponse:
        try:
            draft = _policy_draft(payload)
            candidate = PolicyCandidate(2_147_483_647, "preview", draft.name, 1, draft.priority, draft.matchers, draft.scope_mode, draft.source_ids, draft.channel_ids, draft.repeat_interval_seconds)
            result = store.preview_policy(candidate)
            return NotificationPolicyPreviewResponse(
                direct_match_count=result.direct_match_count,
                shadowed_count=result.shadowed_count,
                final_match_count=result.final_match_count,
                firing_match_count=result.firing_match_count,
                unrouted_count=result.unrouted_count,
                disabled_channel_count=result.disabled_channel_count,
                samples=[
                    NotificationPolicyPreviewSampleResponse(
                        incident_id=item.incident_id,
                        winner_policy_id=item.winner_policy_id,
                        winner_policy_name=item.winner_policy_name,
                    )
                    for item in result.samples
                ],
            )
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-policies/{revision_id}/prepare-activation", response_model=NotificationPolicyActivationPrepareResponse, responses=errors)
    async def prepare_policy(revision_id: int, payload: NotificationPolicyActivationPrepareInput) -> NotificationPolicyActivationPrepareResponse:
        try:
            value = PreparePolicyActivation(store, activation_tokens).execute(revision_id, expected_version=payload.expected_version, notify_existing=payload.notify_existing)
            return NotificationPolicyActivationPrepareResponse(confirm_token=value.confirm_token, eligible_incident_count=value.eligible_incident_count, expires_at_epoch=value.expires_at_epoch)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-policies/{revision_id}/activate", response_model=NotificationPolicyResponse, responses=errors)
    async def activate_policy(revision_id: int, payload: NotificationPolicyActivationInput) -> NotificationPolicyResponse:
        try:
            return _policy(ConfirmPolicyActivation(store, activation_tokens).execute(revision_id, expected_version=payload.expected_version, notify_existing=payload.notify_existing, confirm_token=payload.confirm_token, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-policies/{revision_id}/disable", response_model=NotificationPolicyResponse, responses=errors)
    async def disable_policy(revision_id: int, payload: NotificationPolicyActionInput) -> NotificationPolicyResponse:
        try:
            return _policy(store.disable_policy(revision_id, expected_version=payload.expected_version, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.get("/notification-deliveries", response_model=list[NotificationDeliveryResponse], responses=errors)
    async def list_deliveries(
        state: str | None = Query(default=None, max_length=32),
        event_type: str | None = Query(default=None, max_length=32),
        channel_id: str | None = Query(default=None, max_length=128),
        incident_id: int | None = Query(default=None, ge=1),
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[NotificationDeliveryResponse]:
        return [_delivery(item) for item in store.list_deliveries(state=state, event_type=event_type, channel_id=channel_id, incident_id=incident_id, before_id=before_id, limit=limit)]

    @router.get("/notification-deliveries/{delivery_id}", response_model=NotificationDeliveryResponse, responses=errors)
    async def get_delivery(delivery_id: int) -> NotificationDeliveryResponse:
        try:
            return _delivery(store.get_delivery(delivery_id))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post("/notification-deliveries/{delivery_id}/retry", response_model=NotificationRetryResponse, responses=errors)
    async def retry_delivery(delivery_id: int) -> NotificationRetryResponse:
        try:
            value = store.retry_delivery(delivery_id, now=datetime.now(timezone.utc))
            return NotificationRetryResponse(
                delivery=_delivery(value),
                warning="本次重试遵循 at-least-once 语义；若上一尝试结果不明确，接收方可能看到重复消息。",
            )
        except Exception as exc:
            raise _safe(exc) from exc

    @router.get(
        "/incidents/{incident_id}/notification",
        response_model=IncidentNotificationResponse,
        responses=errors,
    )
    async def get_incident_notification(
        incident_id: int,
    ) -> IncidentNotificationResponse:
        try:
            value = store.get_incident_notification(incident_id)
            route = None
            if value.route is not None:
                route = NotificationRouteResponse(
                    id=value.route.id,
                    status=value.route.status,
                    policy_name=value.route.policy_name,
                    policy_version=value.route.policy_version,
                    repeat_interval_seconds=value.route.repeat_interval_seconds,
                    next_reminder_at=(
                        None
                        if value.route.next_reminder_at is None
                        else to_utc_iso(value.route.next_reminder_at)
                    ),
                    termination_reason=value.route.termination_reason,
                )
            return IncidentNotificationResponse(
                incident_id=value.incident_id,
                occurrence_no=value.occurrence_no,
                route=route,
                targets=[
                    NotificationRouteTargetResponse(
                        id=item.id,
                        channel_id=item.channel_id,
                        channel_name=item.channel_name,
                        provider=item.provider,
                        channel_revision_no=item.channel_revision_no,
                        opened_success_at=(
                            None
                            if item.opened_success_at is None
                            else to_utc_iso(item.opened_success_at)
                        ),
                        last_success_at=(
                            None
                            if item.last_success_at is None
                            else to_utc_iso(item.last_success_at)
                        ),
                    )
                    for item in value.targets
                ],
                deliveries=[_delivery(item) for item in value.deliveries],
            )
        except Exception as exc:
            raise _safe(exc) from exc

    return router
