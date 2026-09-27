"""Web-managed model destinations with save-time activation and diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config_schemas import (
    ModelChannelCreateIn,
    ModelChannelDraftUpdate,
    ModelChannelOut,
    ModelChannelTestOut,
    ModelListOut,
)
from app.crypto import SecretError
from app.providers.model.registry import is_fake_mode
from app.providers.model.vendors import MODEL_VENDORS
from app.db import get_session
from app.registry_models import ModelChannel, ModelChannelRevision
from app.services.model_channels import (
    list_revision_models,
    activate_model_channel_revision,
    create_model_channel,
    disable_model_channel,
    enable_model_channel,
    model_channel_public_dict,
    test_model_channel_revision,
    update_model_channel_draft,
)

router = APIRouter()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SecretError):
        return HTTPException(status_code=503, detail="secret key is unavailable")
    if isinstance(exc, ValueError):
        # Service validation messages are fixed strings and never contain a
        # submitted address or secret.
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(status_code=409, detail="configuration conflict")
    return HTTPException(status_code=500, detail="configuration operation failed")


def _out(session: Session, channel: ModelChannel) -> ModelChannelOut:
    revisions = list(
        session.exec(
            select(ModelChannelRevision)
            .where(ModelChannelRevision.channel_id == channel.id)
            .order_by(ModelChannelRevision.id.desc())
        ).all()
    )
    return ModelChannelOut.model_validate(
        model_channel_public_dict(channel, revisions)
    )


@router.get("/model-channels", response_model=list[ModelChannelOut])
def list_model_channels(
    session: Session = Depends(get_session),
) -> list[ModelChannelOut]:
    channels = session.exec(select(ModelChannel).order_by(ModelChannel.name)).all()
    return [_out(session, item) for item in channels]


@router.post(
    "/model-channels",
    response_model=ModelChannelOut,
    status_code=status.HTTP_201_CREATED,
)
def create_model_channel_endpoint(
    payload: ModelChannelCreateIn,
    session: Session = Depends(get_session),
) -> ModelChannelOut:
    try:
        channel, _revision = create_model_channel(
            session,
            name=payload.name,
            kind=payload.config.kind,
            base_url=payload.config.base_url,
            model=payload.config.model,
            api_key_action=payload.config.api_key.action,
            api_key_value=payload.config.api_key.value,
        )
        session.commit()
        session.refresh(channel)
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.put("/model-channels/{channel_id}/draft", response_model=ModelChannelOut)
def update_model_channel_endpoint(
    channel_id: int,
    payload: ModelChannelDraftUpdate,
    session: Session = Depends(get_session),
) -> ModelChannelOut:
    try:
        update_model_channel_draft(
            session,
            channel_id,
            expected_revision_id=payload.expected_revision_id,
            kind=payload.config.kind,
            base_url=payload.config.base_url,
            model=payload.config.model,
            api_key_action=payload.config.api_key.action,
            api_key_value=payload.config.api_key.value,
        )
        session.commit()
        channel = session.get(ModelChannel, channel_id)
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/model-channel-revisions/{revision_id}/test",
    response_model=ModelChannelTestOut,
)
async def test_model_channel_endpoint(
    revision_id: int,
    session: Session = Depends(get_session),
) -> ModelChannelTestOut:
    try:
        result = await test_model_channel_revision(session, revision_id)
        session.commit()
        return ModelChannelTestOut(
            ok=result.ok,
            code=result.code,
            possibly_billed=result.possibly_billed,
            detail=result.detail,
        )
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/model-channel-revisions/{revision_id}/activate",
    response_model=ModelChannelOut,
)
def activate_model_channel_endpoint(
    revision_id: int,
    session: Session = Depends(get_session),
) -> ModelChannelOut:
    try:
        revision = activate_model_channel_revision(session, revision_id)
        session.commit()
        channel = session.get(ModelChannel, revision.channel_id)
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/model-channels/{channel_id}/disable", response_model=ModelChannelOut)
def disable_model_channel_endpoint(
    channel_id: int,
    session: Session = Depends(get_session),
) -> ModelChannelOut:
    try:
        channel = disable_model_channel(session, channel_id)
        session.commit()
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/model-channels/{channel_id}/enable", response_model=ModelChannelOut)
def enable_model_channel_endpoint(
    channel_id: int,
    session: Session = Depends(get_session),
) -> ModelChannelOut:
    try:
        channel = enable_model_channel(session, channel_id)
        session.commit()
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/model-channel-revisions/{revision_id}/models",
    response_model=ModelListOut,
)
async def list_model_channel_models(
    revision_id: int,
    session: Session = Depends(get_session),
) -> ModelListOut:
    """Populate the model picker from the service the user actually configured.

    POST rather than GET because it causes an outbound request; it is free and
    read-only upstream, but "this button talks to the internet" should not look
    like a cacheable fetch.
    """
    try:
        result = await list_revision_models(session, revision_id)
        return ModelListOut(
            ok=result.ok,
            code=result.code,
            detail=result.detail,
            models=result.models or [],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


class ModelVendorOut(BaseModel):
    id: str
    label: str
    base_url: str
    key_hint: str
    recommended_models: list[str]


class ModelRuntimeOut(BaseModel):
    """Whether a call here would actually reach the configured service.

    The default local-safe mode and `--mock` both substitute a fake client for
    every kind, so a channel can read 已启用 while nothing it named was ever
    contacted. Neither mode may reach a real service, and the UI must expose
    that runtime fact.
    """

    fake_mode: bool


@router.get("/model-runtime", response_model=ModelRuntimeOut)
def get_model_runtime() -> ModelRuntimeOut:
    return ModelRuntimeOut(fake_mode=is_fake_mode())


@router.get("/model-vendors", response_model=list[ModelVendorOut])
def list_model_vendors() -> list[ModelVendorOut]:
    """The vendor presets, so the address is a choice rather than a typing task.

    Served from the backend rather than hardcoded in the page because it is the
    backend that has to reach these hosts: a preset the egress guard would
    reject has no business being offered.
    """
    return [
        ModelVendorOut(
            id=vendor.id,
            label=vendor.label,
            base_url=vendor.base_url,
            key_hint=vendor.key_hint,
            recommended_models=list(vendor.recommended_models),
        )
        for vendor in MODEL_VENDORS
    ]
