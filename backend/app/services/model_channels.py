"""Model channels with atomic activation and non-blocking diagnostics."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, select

from app import master_key as master_key_module
from app.crypto import LazySecretBox, SecretBox, apply_secret_update
from app.registry_models import ModelChannel, ModelChannelRevision
from app.models import ConfigAudit
from app.providers.egress import assert_https_shape
from app.providers.model.base import ModelCallError, ModelClient
from app.providers.model.egress import (
    MODEL_KIND_OPENAI_COMPATIBLE,
    EgressRejected,
    assert_model_egress,
)
from app.providers.model.openai_compatible import (
    OpenAICompatibleConfig,
    list_models,
)
from app.providers.model.registry import get_model_client, supported_kinds
from app.services.investigation_contract import (
    InvestigationContractError,
    structured_response_format,
    validate_investigation_result,
)

SYNTHETIC_FACT_ID = "synthetic_metric_fact_001"
SYNTHETIC_HYPOTHESIS = "The synthetic metric fact supports the synthetic alert."
MAX_CHANNEL_NAME_LENGTH = 120
MAX_BASE_URL_LENGTH = 2048
MAX_MODEL_NAME_LENGTH = 256


@dataclass(frozen=True)
class ModelChannelTestResult:
    ok: bool
    code: str
    possibly_billed: bool
    #: The already-safe sub-code (an HTTP status, an egress rejection reason).
    #: Without it every upstream problem reads as one undifferentiated
    #: SERVICE_ERROR — the exact defect stage 1 hit and fixed in `bd21c07`,
    #: repeated here because the classification and its rendering were built by
    #: different hands. A taxonomy nobody can see is a taxonomy that does not
    #: exist.
    detail: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _box(box: SecretBox | None) -> SecretBox | LazySecretBox:
    return box or LazySecretBox(lambda: master_key_module.master_key())


def _safe_json(value: str, *, code: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError(code) from None
    if not isinstance(parsed, dict):
        raise ValueError(code)
    return parsed


def _dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _clean_config(kind: str, base_url: str, model: str) -> dict[str, str]:
    clean_kind = str(kind or "").strip().upper()
    if clean_kind not in supported_kinds():
        raise ValueError("unsupported model channel kind")
    clean_url = str(base_url or "").strip().rstrip("/")
    clean_model = str(model or "").strip()
    if not clean_url or len(clean_url) > MAX_BASE_URL_LENGTH:
        raise ValueError("model base URL is invalid")
    if not clean_model or len(clean_model) > MAX_MODEL_NAME_LENGTH:
        raise ValueError("model name must contain 1-256 characters")
    try:
        parts = urlsplit(clean_url)
        host, _port = assert_https_shape(clean_url)
        if parts.query or parts.fragment:
            raise EgressRejected("EGRESS_URL_SHAPE")
        # Literal addresses need no DNS and can be rejected at save time.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            assert_model_egress(
                clean_url,
                kind=clean_kind,
                resolver=lambda _host: [host],
            )
    except (EgressRejected, ValueError):
        raise ValueError("model base URL is not allowed") from None
    return {"base_url": clean_url, "model": clean_model}


def _audit(
    session: Session,
    channel: ModelChannel,
    action: str,
    result: str,
    changes: dict[str, Any] | None = None,
) -> None:
    session.add(
        ConfigAudit(
            resource_type="MODEL_CHANNEL",
            resource_id=str(channel.id),
            action=action,
            result=result,
            redacted_diff_json=dict(changes or {}),
        )
    )


def _latest_revisions(
    session: Session, channel_id: int
) -> list[ModelChannelRevision]:
    return list(
        session.exec(
            select(ModelChannelRevision)
            .where(ModelChannelRevision.channel_id == channel_id)
            .order_by(ModelChannelRevision.id.desc())
        ).all()
    )


def create_model_channel(
    session: Session,
    *,
    name: str,
    kind: str,
    base_url: str,
    model: str,
    api_key_action: str,
    api_key_value: str | None,
    box: SecretBox | None = None,
) -> tuple[ModelChannel, ModelChannelRevision]:
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > MAX_CHANNEL_NAME_LENGTH:
        raise ValueError("channel name must contain 1-120 characters")
    if session.exec(
        select(ModelChannel).where(ModelChannel.name == clean_name)
    ).first() is not None:
        raise FileExistsError("model channel name already exists")
    config = _clean_config(kind, base_url, model)
    secret = apply_secret_update(
        None,
        api_key_action,
        api_key_value,
        _box(box),
        required=True,
    )
    channel = ModelChannel(name=clean_name, kind=str(kind).strip().upper())
    session.add(channel)
    session.flush()
    revision = ModelChannelRevision(
        channel_id=int(channel.id),
        state="DRAFT",
        config_envelope_json=_dump_json(config),
        secret_envelope_json=_dump_json(secret or {}),
    )
    session.add(revision)
    session.flush()
    session.refresh(channel)
    session.refresh(revision)
    _audit(
        session,
        channel,
        "CREATE_DRAFT",
        "SUCCESS",
        {"kind": channel.kind, "model": config["model"]},
    )
    activate_model_channel_revision(session, revision.id, box=box)
    session.flush()
    return channel, revision


def update_model_channel_draft(
    session: Session,
    channel_id: int | None,
    *,
    expected_revision_id: int,
    kind: str,
    base_url: str,
    model: str,
    api_key_action: str,
    api_key_value: str | None,
    box: SecretBox | None = None,
) -> ModelChannelRevision:
    channel = session.get(ModelChannel, channel_id)
    if channel is None:
        raise LookupError("model channel not found")
    if str(kind or "").strip().upper() != channel.kind:
        raise ValueError("model channel kind cannot be changed")
    revisions = _latest_revisions(session, int(channel.id))
    if not revisions or revisions[0].id != expected_revision_id:
        raise FileExistsError("model channel revision conflict")
    config = _clean_config(channel.kind, base_url, model)
    draft = next((item for item in revisions if item.state == "DRAFT"), None)
    source = draft or revisions[0]
    existing_secret = _safe_json(
        source.secret_envelope_json, code="model secret envelope is invalid"
    )
    secret = apply_secret_update(
        existing_secret,
        api_key_action,
        api_key_value,
        _box(box),
        required=True,
    )
    if draft is None:
        draft = ModelChannelRevision(
            channel_id=int(channel.id),
            state="DRAFT",
            config_envelope_json=_dump_json(config),
            secret_envelope_json=_dump_json(secret or {}),
        )
    else:
        draft.config_envelope_json = _dump_json(config)
        draft.secret_envelope_json = _dump_json(secret or {})
        draft.tested_ok_at = None
    session.add(draft)
    session.flush()
    session.refresh(draft)
    _audit(
        session,
        channel,
        "UPDATE_DRAFT",
        "SUCCESS",
        {"kind": channel.kind, "model": config["model"]},
    )
    activate_model_channel_revision(session, draft.id, box=box)
    session.flush()
    return draft


def resolve_model_revision(
    revision: ModelChannelRevision,
    *,
    kind: str,
    box: SecretBox | None = None,
) -> OpenAICompatibleConfig:
    config = _safe_json(
        revision.config_envelope_json, code="model config envelope is invalid"
    )
    clean = _clean_config(
        kind,
        str(config.get("base_url") or ""),
        str(config.get("model") or ""),
    )
    secret = _safe_json(
        revision.secret_envelope_json, code="model secret envelope is invalid"
    )
    api_key = _box(box).decrypt(secret)
    return OpenAICompatibleConfig(
        base_url=clean["base_url"],
        api_key=api_key,
        model=clean["model"],
    )


def _synthetic_messages() -> list[dict[str, Any]]:
    payload = {
        "alert": {
            "alertname": "SyntheticModelChannelTest",
            "labels": {"severity": "info", "scope": "synthetic"},
            "annotations": {
                "summary": "Synthetic configuration test; no real alert data."
            },
        },
        "facts": [
            {
                "fact_id": SYNTHETIC_FACT_ID,
                "statement": "The synthetic metric increased after the synthetic alert.",
            }
        ],
        "required_result": {
            "statement": SYNTHETIC_HYPOTHESIS,
            "verdict": "SUPPORTED",
            "supporting_fact_ids": [SYNTHETIC_FACT_ID],
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "Return only the requested structured investigation result. "
                "This is a synthetic configuration test."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


async def test_model_channel_revision(
    session: Session,
    revision_id: int | None,
    *,
    box: SecretBox | None = None,
    client: ModelClient | None = None,
) -> ModelChannelTestResult:
    revision = session.get(ModelChannelRevision, revision_id)
    if revision is None or revision.state not in {"DRAFT", "ACTIVE"}:
        raise LookupError("current model channel revision not found")
    channel = session.get(ModelChannel, revision.channel_id)
    if channel is None:
        raise LookupError("model channel not found")
    runtime = resolve_model_revision(revision, kind=channel.kind, box=box)
    active_client = client or get_model_client(channel.kind, runtime)
    try:
        reply = await active_client.complete(
            messages=_synthetic_messages(),
            tools=(),
            response_format=structured_response_format(),
        )
        result = validate_investigation_result(
            reply.text, available_fact_ids={SYNTHETIC_FACT_ID}
        )
        hypothesis = result.hypotheses[0]
        if (
            len(result.hypotheses) != 1
            or hypothesis.statement != SYNTHETIC_HYPOTHESIS
            or hypothesis.verdict != "SUPPORTED"
            or hypothesis.supporting_fact_ids != [SYNTHETIC_FACT_ID]
        ):
            raise InvestigationContractError("synthetic contract mismatch")
    except ModelCallError as exc:
        revision.tested_ok_at = None
        session.add(revision)
        _audit(
            session,
            channel,
            "TEST",
            "FAILED",
            {
                "code": exc.kind.value,
                "detail": exc.detail,
                "possibly_billed": exc.possibly_billed,
            },
        )
        session.flush()
        return ModelChannelTestResult(
            ok=False,
            code=exc.kind.value,
            possibly_billed=exc.possibly_billed,
            detail=exc.detail,
        )
    except InvestigationContractError:
        revision.tested_ok_at = None
        session.add(revision)
        _audit(
            session,
            channel,
            "TEST",
            "FAILED",
            {"code": "CONTRACT_INVALID", "possibly_billed": True},
        )
        session.flush()
        return ModelChannelTestResult(
            ok=False, code="CONTRACT_INVALID", possibly_billed=True
        )

    revision.tested_ok_at = _now()
    session.add(revision)
    _audit(
        session,
        channel,
        "TEST",
        "SUCCESS",
        {"code": "OK", "possibly_billed": True},
    )
    session.flush()
    return ModelChannelTestResult(ok=True, code="OK", possibly_billed=True)


test_model_channel_revision.__test__ = False


def activate_model_channel_revision(
    session: Session,
    revision_id: int | None,
    *,
    box: SecretBox | None = None,
) -> ModelChannelRevision:
    revision = session.get(ModelChannelRevision, revision_id)
    if revision is None or revision.state not in {"DRAFT", "ACTIVE"}:
        raise LookupError("current model channel revision not found")
    if not _safe_json(
        revision.config_envelope_json, code="model config envelope is invalid"
    ).get("model"):
        raise ValueError("model channel requires a model before activation")
    channel = session.get(ModelChannel, revision.channel_id)
    if channel is None:
        raise LookupError("model channel not found")
    # Fail closed if the encrypted secret cannot be opened with the current key.
    resolve_model_revision(revision, kind=channel.kind, box=box)
    active = session.exec(
        select(ModelChannelRevision).where(
            ModelChannelRevision.channel_id == channel.id,
            ModelChannelRevision.state == "ACTIVE",
        )
    ).all()
    for item in active:
        if item.id == revision.id:
            continue
        item.state = "RETIRED"
        session.add(item)
    revision.state = "ACTIVE"
    # Exactly one model service is in use, so make that true in the data rather
    # than explaining it in a tooltip. `resolve_active_model` returns one channel
    # (lowest id wins), so leaving an older one enabled meant the user could
    # configure a new service, see it marked 已启用, and have every call keep
    # going to the old one — with no screen anywhere that would reveal it.
    for other in session.exec(
        select(ModelChannel).where(
            ModelChannel.enabled == True,  # noqa: E712
            ModelChannel.id != channel.id,
        )
    ).all():
        other.enabled = False
        session.add(other)
        _audit(session, other, "DISABLE", "SUCCESS", {"reason": "superseded"})
    channel.enabled = True
    channel.updated_at = _now()
    session.add(revision)
    session.add(channel)
    _audit(session, channel, "ACTIVATE", "SUCCESS", {"revision_id": revision.id})
    session.flush()
    return revision


def disable_model_channel(
    session: Session, channel_id: int | None
) -> ModelChannel:
    channel = session.get(ModelChannel, channel_id)
    if channel is None:
        raise LookupError("model channel not found")
    channel.enabled = False
    channel.updated_at = _now()
    session.add(channel)
    _audit(session, channel, "DISABLE", "SUCCESS")
    session.flush()
    return channel


def enable_model_channel(
    session: Session,
    channel_id: int | None,
    *,
    box: SecretBox | None = None,
) -> ModelChannel:
    channel = session.get(ModelChannel, channel_id)
    if channel is None:
        raise LookupError("model channel not found")
    active = session.exec(
        select(ModelChannelRevision).where(
            ModelChannelRevision.channel_id == channel.id,
            ModelChannelRevision.state == "ACTIVE",
        )
    ).first()
    if active is None:
        raise ValueError("model channel has no active revision")
    resolve_model_revision(active, kind=channel.kind, box=box)
    channel.enabled = True
    channel.updated_at = _now()
    session.add(channel)
    _audit(session, channel, "ENABLE", "SUCCESS")
    session.flush()
    return channel


def model_channel_public_dict(
    channel: ModelChannel,
    revisions: list[ModelChannelRevision],
) -> dict[str, Any]:
    def revision_dict(item: ModelChannelRevision) -> dict[str, Any]:
        config = _safe_json(
            item.config_envelope_json, code="model config envelope is invalid"
        )
        base_url = str(config.get("base_url") or "")
        return {
            "id": item.id,
            "state": item.state,
            "base_url": base_url,
            "base_host": urlsplit(base_url).hostname or "",
            "model": str(config.get("model") or ""),
            "secret_configured": bool(item.secret_envelope_json),
            "tested_ok_at": item.tested_ok_at,
            "created_at": item.created_at,
        }

    active = next((item for item in revisions if item.state == "ACTIVE"), None)
    return {
        "id": channel.id,
        "name": channel.name,
        "kind": channel.kind,
        "enabled": channel.enabled,
        "active_revision_id": active.id if active else None,
        "created_at": channel.created_at,
        "updated_at": channel.updated_at,
        "revisions": [revision_dict(item) for item in revisions],
    }


__all__ = [
    "ModelChannelTestResult",
    "SYNTHETIC_FACT_ID",
    "SYNTHETIC_HYPOTHESIS",
    "activate_model_channel_revision",
    "create_model_channel",
    "disable_model_channel",
    "enable_model_channel",
    "model_channel_public_dict",
    "resolve_model_revision",
    "test_model_channel_revision",
    "update_model_channel_draft",
]


@dataclass(frozen=True)
class ActiveModel:
    """The channel a real run would use: how to describe it, and how to call it.

    One object rather than two lookups. The egress preview (D51) must name the
    destination that the *next* call will actually use, and two separate
    functions each doing their own query is a seam where the description and the
    client can disagree -- the preview would then be a lie precisely when it
    matters.

    Carries the host, never the URL and never the credential: a preview that
    leaked the key would be the thing it exists to warn about.
    """

    channel_id: int
    name: str
    host: str
    model: str
    config: OpenAICompatibleConfig

    def client(self) -> ModelClient:
        return get_model_client("OPENAI_COMPATIBLE", self.config)


def resolve_active_model(
    session: Session, *, box: SecretBox | None = None
) -> ActiveModel | None:
    """The one lookup. None when no saved-and-enabled service exists.

    Saving atomically creates an ACTIVE revision; the optional connection test
    is an independent diagnostic and never controls whether this lookup answers.

    Returns None rather than raising: "no model configured" is an ordinary state
    of this product, and the caller turns it into a sentence, not a stack trace.
    """

    channel = session.exec(
        select(ModelChannel)
        .where(ModelChannel.enabled == True)  # noqa: E712 - SQL, not truthiness
        .order_by(ModelChannel.id.asc())
    ).first()
    if channel is None:
        return None
    active = next(
        (
            revision
            for revision in _latest_revisions(session, channel.id)
            if revision.state == "ACTIVE"
        ),
        None,
    )
    if active is None:
        return None
    config = resolve_model_revision(active, kind=channel.kind, box=box)
    return ActiveModel(
        channel_id=int(channel.id or 0),
        name=channel.name,
        host=urlsplit(config.base_url).hostname or "",
        model=config.model,
        config=config,
    )


@dataclass
class ModelListResult:
    ok: bool
    code: str
    detail: str = ""
    models: list[str] | None = None


async def list_revision_models(
    session: Session,
    revision_id: int | None,
    *,
    box: SecretBox | None = None,
    lister=None,
) -> ModelListResult:
    """Ask the configured service which models it offers.

    Read-only, so this is safe behind an explicit button that just fills a
    picker. It needs a saved revision because the encrypted key never
    round-trips through the browser. The request does not alter enabled state.
    """

    revision = session.get(ModelChannelRevision, revision_id)
    if revision is None:
        raise LookupError("model channel revision not found")
    channel = session.get(ModelChannel, revision.channel_id)
    if channel is None:
        raise LookupError("model channel not found")

    runtime = resolve_model_revision(revision, kind=channel.kind, box=box)
    call = lister or list_models
    try:
        names = await call(runtime)
    except ModelCallError as exc:
        return ModelListResult(ok=False, code=exc.kind.value, detail=exc.detail)
    return ModelListResult(ok=True, code="OK", models=list(names))
