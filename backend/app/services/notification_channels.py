"""Logical Feishu channels with isolated draft/active revisions."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any, Protocol

from pydantic import ValidationError
from sqlmodel import Session, select

from app import master_key as master_key_module
from app.crypto import LazySecretBox, SecretBox, apply_secret_update, redact_sensitive
from app.models import (
    ConfigAudit,
    NotificationChannel,
    NotificationChannelRevision,
    NotificationDelivery,
    NotificationRouteTarget,
)
from app.providers.configs import (
    FeishuChannelConfig,
    parse_channel_config,
    resolve_feishu,
)
from app.providers.configs import (
    GenericWebhookChannelConfig,
    SmtpChannelConfig,
)
from app.providers.registry import (
    FEISHU_CUSTOM_BOT,
    GENERIC_WEBHOOK,
    SMTP,
    SUPPORTED_KINDS,
)
from app.providers.feishu import (
    FeishuConfig,
    FeishuProvider,
    ProviderResult,
    is_valid_open_id,
    validate_feishu_webhook,
)


class ChannelProvider(Protocol):
    async def send(
        self, payload: dict[str, Any], config: FeishuConfig, **kwargs: Any
    ) -> ProviderResult: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _box(box: SecretBox | None) -> SecretBox | LazySecretBox:
    # Lazy: a resource with no credentials must be savable without a master key.
    return box or LazySecretBox(lambda: master_key_module.master_key())


def _validate_channel_values(
    *,
    required_keyword: str | None,
    mention_mode: str,
    mention_users: list[dict[str, str]],
    mention_on: dict[str, bool],
) -> dict[str, Any]:
    keyword = str(required_keyword or "").strip() or None
    if keyword is not None and len(keyword) > 64:
        raise ValueError("required_keyword is too long")
    mode = str(mention_mode or "NONE").upper()
    if mode not in {"NONE", "USERS", "ALL"}:
        raise ValueError("mention_mode must be NONE, USERS, or ALL")
    users: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in mention_users or []:
        if set(item) != {"open_id"}:
            raise ValueError("mention users accept open_id only")
        open_id = str(item.get("open_id") or "").strip()
        if not is_valid_open_id(open_id):
            raise ValueError("mention open_id is invalid")
        if open_id not in seen:
            users.append({"open_id": open_id})
            seen.add(open_id)
    if mode == "USERS" and not users:
        raise ValueError("USERS mention mode requires at least one open_id")
    allowed_events = {
        "FIRING_OPENED",
        "SEVERITY_ESCALATED",
        "REMINDER",
        "RECOVERED",
    }
    event_map = {str(key): bool(value) for key, value in (mention_on or {}).items()}
    if not set(event_map) <= allowed_events:
        raise ValueError("mention_on contains unsupported event type")
    return {
        "required_keyword": keyword,
        "mention_mode": mode,
        "mention_users": users,
        "mention_on": event_map,
    }


def _audit(
    session: Session,
    channel: NotificationChannel,
    action: str,
    result: str,
    changes: dict[str, Any] | None = None,
) -> None:
    session.add(
        ConfigAudit(
            resource_type="NOTIFICATION_CHANNEL",
            resource_id=str(channel.id),
            action=action,
            result=result,
            redacted_diff_json=redact_sensitive(changes or {}),
        )
    )


def _safe_config_error(exc: ValidationError) -> str:
    """A validation message that names the field but never repeats the value.

    `str(ValidationError)` embeds the offending input, so a rejected internal
    address comes straight back out of the API. Same lesson as
    `sources/thanos.py:safe_error_code`: what leaves the backend is a code and a
    field, not the thing the user typed.
    """

    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "kind")
        message = str(error.get("msg") or "invalid value")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "invalid channel configuration"



def build_provider_config(
    provider: str,
    draft: dict[str, Any],
    previous: dict[str, Any] | None,
    secret_box: SecretBox | LazySecretBox,
) -> dict[str, Any]:
    """Turn a submitted draft into the stored per-provider config.

    Secret fields arrive as `{action, value}` and are resolved against whatever
    the previous revision held, so `KEEP` keeps working for a credential the user
    cannot read back. Everything else is validated by the kind's own model.
    """

    kind = str(provider or FEISHU_CUSTOM_BOT).strip().upper()
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported channel provider: {kind}")
    prior = dict(previous or {})
    values = dict(draft or {})

    def resolve(field: str, *, required: bool) -> dict[str, Any] | None:
        update = values.pop(field, None) or {"action": "KEEP"}
        return apply_secret_update(
            prior.get(field),
            str(update.get("action") or "KEEP"),
            update.get("value"),
            secret_box,
            required=required,
        )

    if kind == FEISHU_CUSTOM_BOT:
        webhook_update = dict(values.get("webhook") or {})
        webhook = resolve("webhook", required=True)
        if (
            str(webhook_update.get("action") or "").upper() == "REPLACE"
            and webhook_update.get("value") is not None
        ):
            validate_feishu_webhook(str(webhook_update["value"]))
        checked = _validate_channel_values(
            required_keyword=values.get("required_keyword"),
            mention_mode=values.get("mention_mode", "NONE"),
            mention_users=values.get("mention_users", []),
            mention_on=values.get("mention_on", {}),
        )
        try:
            return FeishuChannelConfig(
                webhook=webhook or {},
                signing_secret=resolve("signing_secret", required=False),
                **checked,
            ).model_dump()
        except ValidationError as exc:
            raise ValueError(_safe_config_error(exc)) from None

    if kind == SMTP:
        try:
            return SmtpChannelConfig(
                host=values.get("host", ""),
                port=int(values.get("port") or 0),
                tls_mode=values.get("tls_mode", "STARTTLS"),
                username=values.get("username", ""),
                password=resolve("password", required=False),
                from_addr=values.get("from_addr", ""),
                to_addrs=values.get("to_addrs", []),
                subject_prefix=values.get("subject_prefix", ""),
            ).model_dump()
        except ValidationError as exc:
            raise ValueError(_safe_config_error(exc)) from None

    try:
        return GenericWebhookChannelConfig(
            url=values.get("url", ""),
            headers=resolve("headers", required=False),
            signing_secret=resolve("signing_secret", required=False),
            timeout_seconds=float(values.get("timeout_seconds") or 8.0),
        ).model_dump()
    except ValidationError as exc:
        raise ValueError(_safe_config_error(exc)) from None


def config_summary(provider: str, stored: dict[str, Any]) -> dict[str, Any]:
    """What the UI may show about a channel's configuration.

    Never a secret and never a full address: enough to recognise which channel
    this is, and nothing an onlooker could reuse.
    """

    kind = str(provider or FEISHU_CUSTOM_BOT).strip().upper()
    config = dict(stored or {})
    if kind == SMTP:
        recipients = config.get("to_addrs") or []
        return {
            "kind": kind,
            "target": f"{config.get('host', '')}:{config.get('port', '')}",
            "detail": f"{len(recipients)} 个收件人",
            "secret_configured": bool(config.get("password")),
        }
    if kind == GENERIC_WEBHOOK:
        url = str(config.get("url") or "")
        host = urlsplit(url).hostname or ""
        return {
            "kind": kind,
            "target": host,
            "detail": "自定义请求头已配置" if config.get("headers") else "无自定义请求头",
            "secret_configured": bool(config.get("signing_secret")),
        }
    return {
        "kind": FEISHU_CUSTOM_BOT,
        "target": "飞书群机器人",
        "detail": {"NONE": "不 @", "ALL": "@all", "USERS": "@ 指定成员"}.get(
            str(config.get("mention_mode") or "NONE"), "不 @"
        ),
        "secret_configured": bool(config.get("signing_secret")),
    }



def create_channel(
    session: Session,
    *,
    name: str,
    provider: str = FEISHU_CUSTOM_BOT,
    config: dict[str, Any] | None = None,
    webhook_action: str = "CLEAR",
    webhook_value: str | None = None,
    signing_action: str = "CLEAR",
    signing_value: str | None = None,
    required_keyword: str | None = None,
    mention_mode: str = "NONE",
    mention_users: list[dict[str, str]] | None = None,
    mention_on: dict[str, bool] | None = None,
    box: SecretBox | None = None,
) -> tuple[NotificationChannel, NotificationChannelRevision]:
    """Create a channel of any supported kind.

    `config` is the general path, one shape per provider. The Feishu keyword
    arguments below it are the shorthand the API used before F24 and that most
    tests still use; they build the same config.
    """

    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("channel name must contain 1-120 characters")
    if session.exec(
        select(NotificationChannel).where(NotificationChannel.name == clean_name)
    ).first() is not None:
        raise FileExistsError("channel name already exists")
    secret_box = _box(box)
    draft = config if config is not None else {
        "webhook": {"action": webhook_action, "value": webhook_value},
        "signing_secret": {"action": signing_action, "value": signing_value},
        "required_keyword": required_keyword,
        "mention_mode": mention_mode,
        "mention_users": mention_users or [],
        "mention_on": mention_on or {},
    }
    stored = build_provider_config(provider, draft, None, secret_box)
    # The five Feishu columns still carry KEEP resolution and the API's
    # "configured" flags; for other kinds they stay empty, which is what
    # `webhook_configured` should report for a channel that has no webhook.
    values = {
        "required_keyword": stored.get("required_keyword"),
        "mention_mode": stored.get("mention_mode", "NONE"),
        "mention_users": stored.get("mention_users", []),
        "mention_on": stored.get("mention_on", {}),
    }
    channel = NotificationChannel(name=clean_name)
    session.add(channel)
    session.flush()
    revision = NotificationChannelRevision(
        channel_id=channel.id,
        version=1,
        state="DRAFT",
        provider=str(provider or FEISHU_CUSTOM_BOT).strip().upper(),
        webhook_envelope=stored.get("webhook") or {},
        signing_secret_envelope=stored.get("signing_secret"),
        config_envelope=stored,
        **values,
    )
    session.add(revision)
    session.flush()
    session.refresh(channel)
    session.refresh(revision)
    _audit(session, channel, "CREATE_DRAFT", "SUCCESS", values)
    session.flush()
    return channel, revision


def update_channel_draft(
    session: Session,
    channel_id: int | None,
    *,
    expected_version: int,
    config: dict[str, Any] | None = None,
    webhook_action: str = "KEEP",
    webhook_value: str | None = None,
    signing_action: str = "KEEP",
    signing_value: str | None = None,
    required_keyword: str | None = None,
    mention_mode: str = "NONE",
    mention_users: list[dict[str, str]] | None = None,
    mention_on: dict[str, bool] | None = None,
    box: SecretBox | None = None,
) -> NotificationChannelRevision:
    """Update the draft revision. A channel's provider never changes: a different
    kind is a different destination, the same as a different group."""

    channel = session.get(NotificationChannel, channel_id)
    if channel is None:
        raise LookupError("channel not found")
    revisions = session.exec(
        select(NotificationChannelRevision)
        .where(NotificationChannelRevision.channel_id == channel.id)
        .order_by(NotificationChannelRevision.version.desc())
    ).all()
    current = revisions[0]
    if current.version != expected_version:
        raise FileExistsError("channel revision conflict")
    draft = next((item for item in revisions if item.state == "DRAFT"), None)
    source = draft or current
    secret_box = _box(box)
    submitted = config if config is not None else {
        "webhook": {"action": webhook_action, "value": webhook_value},
        "signing_secret": {"action": signing_action, "value": signing_value},
        "required_keyword": required_keyword,
        "mention_mode": mention_mode,
        "mention_users": mention_users or [],
        "mention_on": mention_on or {},
    }
    provider = source.provider or FEISHU_CUSTOM_BOT
    if config is not None and str(
        submitted.get("kind") or provider
    ).strip().upper() != provider:
        raise ValueError("channel provider cannot be changed; create a new channel")
    stored = build_provider_config(
        provider, submitted, source.config_envelope or {}, secret_box
    )
    if provider == FEISHU_CUSTOM_BOT and str(
        (submitted.get("webhook") or {}).get("action") or ""
    ).upper() == "REPLACE":
        validate_feishu_webhook(str((submitted.get("webhook") or {}).get("value") or ""))
    webhook = stored.get("webhook") or {}
    signing = stored.get("signing_secret")
    values = {
        "required_keyword": stored.get("required_keyword"),
        "mention_mode": stored.get("mention_mode", "NONE"),
        "mention_users": stored.get("mention_users", []),
        "mention_on": stored.get("mention_on", {}),
    }
    if draft is None:
        draft = NotificationChannelRevision(
            channel_id=channel.id,
            version=current.version + 1,
            state="DRAFT",
            provider=provider,
            webhook_envelope=webhook or {},
            signing_secret_envelope=signing,
            config_envelope=stored,
            **values,
        )
    else:
        draft.provider = provider
        draft.webhook_envelope = webhook or {}
        draft.signing_secret_envelope = signing
        draft.config_envelope = stored
        for key, value in values.items():
            setattr(draft, key, value)
        draft.last_tested_at = None
        draft.last_test_status = None
        draft.last_test_error_code = None
        draft.updated_at = _now()
    session.add(draft)
    session.flush()
    session.refresh(draft)
    _audit(session, channel, "UPDATE_DRAFT", "SUCCESS", values)
    session.flush()
    return draft


def _revision_config(
    revision: NotificationChannelRevision, box: SecretBox
) -> FeishuConfig:
    """Credentials for one revision, from `config_envelope` where it exists.

    The fallback to the legacy columns is not dead code: a revision written
    before migration v9 ran, or by an older build against the same database,
    still has to send. v9 backfills, so the fallback should be unreachable in
    practice -- it is here so that "unreachable" is not load-bearing.
    """

    stored = revision.config_envelope or {}
    if stored:
        config = parse_channel_config(
            revision.provider or FEISHU_CUSTOM_BOT, stored
        )
        if isinstance(config, FeishuChannelConfig):
            return resolve_feishu(config, box.decrypt)
        raise ValueError(f"unsupported channel provider: {config.kind}")

    webhook = box.decrypt(revision.webhook_envelope)
    signing_secret = (
        box.decrypt(revision.signing_secret_envelope)
        if revision.signing_secret_envelope is not None
        else ""
    )
    return FeishuConfig(
        webhook=validate_feishu_webhook(webhook),
        signing_secret=signing_secret,
        required_keyword=revision.required_keyword,
    )


def resolve_active_channel_config(
    session: Session,
    channel_id: int,
    *,
    box: SecretBox | None = None,
) -> tuple[NotificationChannelRevision, FeishuConfig]:
    """Resolve current credentials for a routed logical channel at send time."""
    channel = session.get(NotificationChannel, channel_id)
    if channel is None or channel.state != "ENABLED":
        raise ValueError("channel is disabled or unavailable")
    revision = session.get(NotificationChannelRevision, channel.active_revision_id)
    if revision is None or revision.state != "ACTIVE":
        raise ValueError("channel has no active credential revision")
    return revision, _revision_config(revision, _box(box))


def _test_payload(revision: NotificationChannelRevision) -> dict[str, Any]:
    keyword = revision.required_keyword or "Alert Workbench"
    mention_text = ""
    if revision.mention_mode == "ALL":
        mention_text = " <at id=all></at>"
    elif revision.mention_mode == "USERS":
        # Validated on save, re-checked here: rows stored before the character
        # set was pinned are still in the database.
        mention_text = " " + " ".join(
            f'<at id={item["open_id"]}></at>'
            for item in revision.mention_users
            if is_valid_open_id(item.get("open_id"))
        )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"{keyword} 通道测试"}
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "配置测试成功到达请求阶段。" + mention_text,
                    },
                }
            ],
        },
    }


async def test_channel_revision(
    session: Session,
    revision_id: int | None,
    *,
    box: SecretBox | None = None,
    provider: ChannelProvider | None = None,
) -> ProviderResult:
    revision = session.get(NotificationChannelRevision, revision_id)
    if revision is None or revision.state != "DRAFT":
        raise LookupError("draft channel revision not found")
    channel = session.get(NotificationChannel, revision.channel_id)
    if channel is None:
        raise LookupError("channel not found")
    config = _revision_config(revision, _box(box))
    result = await (provider or FeishuProvider()).send(
        _test_payload(revision), config, purpose="CHANNEL_TEST"
    )
    revision.last_tested_at = _now()
    revision.last_test_status = "SUCCESS" if result.ok else "FAILED"
    revision.last_test_error_code = None if result.ok else result.code
    revision.updated_at = _now()
    session.add(revision)
    _audit(
        session,
        channel,
        "TEST",
        "SUCCESS" if result.ok else "FAILED",
        {"revision": revision.version, "code": result.code},
    )
    session.flush()
    return result


test_channel_revision.__test__ = False


def activate_channel_revision(
    session: Session,
    revision_id: int | None,
    *,
    expected_version: int,
    box: SecretBox | None = None,
) -> NotificationChannelRevision:
    revision = session.get(NotificationChannelRevision, revision_id)
    if revision is None or revision.state != "DRAFT":
        raise LookupError("draft channel revision not found")
    if revision.version != expected_version:
        raise FileExistsError("channel revision conflict")
    if revision.last_test_status != "SUCCESS":
        raise ValueError("channel requires a successful test before activation")
    _revision_config(revision, _box(box))  # fail closed before changing state
    channel = session.get(NotificationChannel, revision.channel_id)
    if channel is None:
        raise LookupError("channel not found")
    active = session.exec(
        select(NotificationChannelRevision).where(
            NotificationChannelRevision.channel_id == channel.id,
            NotificationChannelRevision.state == "ACTIVE",
        )
    ).all()
    for item in active:
        item.state = "RETIRED"
        item.updated_at = _now()
        session.add(item)
    session.flush()
    revision.state = "ACTIVE"
    revision.activated_at = _now()
    revision.updated_at = _now()
    channel.active_revision_id = revision.id
    channel.state = "ENABLED"
    channel.updated_at = _now()
    session.add(revision)
    session.add(channel)
    _audit(session, channel, "ACTIVATE", "SUCCESS", {"version": revision.version})
    session.flush()
    return revision


def disable_channel(
    session: Session, channel_id: int | None
) -> NotificationChannel:
    channel = session.get(NotificationChannel, channel_id)
    if channel is None:
        raise LookupError("channel not found")
    channel.state = "DISABLED"
    channel.disabled_at = _now()
    channel.updated_at = _now()
    session.add(channel)
    target_ids = [
        item.id
        for item in session.exec(
            select(NotificationRouteTarget).where(
                NotificationRouteTarget.channel_id == channel.id
            )
        ).all()
        if item.id is not None
    ]
    if target_ids:
        deliveries = session.exec(
            select(NotificationDelivery).where(
                NotificationDelivery.route_target_id.in_(target_ids),
                NotificationDelivery.state.in_(["PENDING", "RETRY_WAIT"]),
            )
        ).all()
        for delivery in deliveries:
            delivery.state = "SUPPRESSED"
            delivery.suppression_reason = "CHANNEL_DISABLED"
            delivery.updated_at = _now()
            session.add(delivery)
    _audit(session, channel, "DISABLE", "SUCCESS")
    session.flush()
    return channel


def enable_channel(
    session: Session, channel_id: int | None, *, box: SecretBox | None = None
) -> NotificationChannel:
    channel = session.get(NotificationChannel, channel_id)
    if channel is None or channel.active_revision_id is None:
        raise ValueError("channel has no active revision")
    revision = session.get(NotificationChannelRevision, channel.active_revision_id)
    if revision is None or revision.state != "ACTIVE":
        raise ValueError("channel active revision is unavailable")
    _revision_config(revision, _box(box))
    channel.state = "ENABLED"
    channel.disabled_at = None
    channel.updated_at = _now()
    session.add(channel)
    _audit(session, channel, "ENABLE", "SUCCESS")
    session.flush()
    return channel


def channel_public_dict(
    channel: NotificationChannel,
    revisions: list[NotificationChannelRevision],
) -> dict[str, Any]:
    def revision_dict(item: NotificationChannelRevision) -> dict[str, Any]:
        return {
            "id": item.id,
            "version": item.version,
            "state": item.state,
            "provider": item.provider,
            "webhook_configured": bool(item.webhook_envelope),
            "signing_secret_configured": item.signing_secret_envelope is not None,
            "config_summary": config_summary(item.provider, item.config_envelope or {}),
            "required_keyword": item.required_keyword,
            "mention_mode": item.mention_mode,
            "mention_users": list(item.mention_users),
            "mention_on": dict(item.mention_on),
            "last_tested_at": item.last_tested_at,
            "last_test_status": item.last_test_status,
            "last_test_error_code": item.last_test_error_code,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "activated_at": item.activated_at,
        }

    return {
        "id": channel.id,
        "name": channel.name,
        "state": channel.state,
        "active_revision_id": channel.active_revision_id,
        "created_at": channel.created_at,
        "updated_at": channel.updated_at,
        "disabled_at": channel.disabled_at,
        "revisions": [revision_dict(item) for item in revisions],
    }
