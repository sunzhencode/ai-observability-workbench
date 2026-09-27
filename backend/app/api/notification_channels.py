"""Notification channel draft/test/activate APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config_schemas import (
    ChannelCreateIn,
    ChannelDraftUpdate,
    ChannelOut,
    RevisionActionIn,
    TestResultOut,
)
from app.crypto import SecretBox, SecretError
from app.db import get_session
from app.models import NotificationChannel, NotificationChannelRevision
from app.providers.feishu import FeishuProvider
from app.providers.runtime import ScriptedFakeFeishuProvider, get_notification_provider
from app.services.notification_channels import (
    activate_channel_revision,
    channel_public_dict,
    create_channel,
    disable_channel,
    enable_channel,
    test_channel_revision,
    update_channel_draft,
)

router = APIRouter()


def get_provider() -> FeishuProvider | ScriptedFakeFeishuProvider:
    return get_notification_provider()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SecretError):
        return HTTPException(status_code=503, detail="secret key is unavailable")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(status_code=409, detail="configuration conflict")
    return HTTPException(status_code=500, detail="configuration operation failed")


def _out(session: Session, channel: NotificationChannel) -> ChannelOut:
    revisions = session.exec(
        select(NotificationChannelRevision)
        .where(NotificationChannelRevision.channel_id == channel.id)
        .order_by(NotificationChannelRevision.version.desc())
    ).all()
    return ChannelOut.model_validate(channel_public_dict(channel, revisions))


@router.get("/notification-channels", response_model=list[ChannelOut])
def list_channels(session: Session = Depends(get_session)) -> list[ChannelOut]:
    channels = session.exec(
        select(NotificationChannel).order_by(NotificationChannel.name)
    ).all()
    return [_out(session, item) for item in channels]


@router.post(
    "/notification-channels",
    response_model=ChannelOut,
    status_code=status.HTTP_201_CREATED,
)
def create_channel_endpoint(
    payload: ChannelCreateIn,
    session: Session = Depends(get_session),
) -> ChannelOut:
    try:
        channel, _revision = create_channel(
            session,
            name=payload.name,
            provider=payload.config.kind,
            config=payload.config.model_dump(),
        )
        session.commit()
        session.refresh(channel)
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.put("/notification-channels/{channel_id}/draft", response_model=ChannelOut)
def update_channel_endpoint(
    channel_id: int,
    payload: ChannelDraftUpdate,
    session: Session = Depends(get_session),
) -> ChannelOut:
    try:
        update_channel_draft(
            session,
            channel_id,
            expected_version=payload.expected_version,
            config=payload.config.model_dump(),
        )
        session.commit()
        channel = session.get(NotificationChannel, channel_id)
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/notification-channel-revisions/{revision_id}/test",
    response_model=TestResultOut,
)
async def test_channel_endpoint(
    revision_id: int,
    session: Session = Depends(get_session),
    provider: FeishuProvider | ScriptedFakeFeishuProvider = Depends(get_provider),
) -> TestResultOut:
    try:
        result = await test_channel_revision(
            session, revision_id, provider=provider
        )
        session.commit()
        return TestResultOut(ok=result.ok, code=result.code, transient=result.transient)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/notification-channel-revisions/{revision_id}/activate",
    response_model=ChannelOut,
)
def activate_channel_endpoint(
    revision_id: int,
    payload: RevisionActionIn,
    session: Session = Depends(get_session),
) -> ChannelOut:
    try:
        revision = activate_channel_revision(
            session, revision_id, expected_version=payload.expected_version
        )
        session.commit()
        channel = session.get(NotificationChannel, revision.channel_id)
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/notification-channels/{channel_id}/disable", response_model=ChannelOut)
def disable_channel_endpoint(
    channel_id: int, session: Session = Depends(get_session)
) -> ChannelOut:
    try:
        channel = disable_channel(session, channel_id)
        session.commit()
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/notification-channels/{channel_id}/enable", response_model=ChannelOut)
def enable_channel_endpoint(
    channel_id: int, session: Session = Depends(get_session)
) -> ChannelOut:
    try:
        channel = enable_channel(session, channel_id)
        session.commit()
        return _out(session, channel)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
