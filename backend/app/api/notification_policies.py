"""Notification policy draft, preview, and confirmed activation APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config_schemas import (
    ConfirmActivationIn,
    PolicyDraftIn,
    PolicyDraftUpdate,
    PolicyOut,
    PolicyPreviewIn,
    PolicyPreviewOut,
    PrepareActivationIn,
    PrepareActivationOut,
)
from app.db import get_session
from app.models import Incident, NotificationChannel, NotificationPolicyRevision
from app.services.notification_policies import (
    PolicyCandidate,
    active_policy_candidates,
    create_policy_draft,
    disable_policy,
    policy_public_dict,
    preview_policy,
    update_policy_draft,
    validate_policy_matchers,
    validate_repeat_interval,
)
from app.services.policy_activation import (
    ActivationTokenError,
    confirm_activation,
    prepare_activation,
)

router = APIRouter()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (FileExistsError, ActivationTokenError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(status_code=409, detail="policy conflict")
    return HTTPException(status_code=500, detail="policy operation failed")


def _matcher_dicts(payload) -> list[dict[str, str]]:
    return [item.model_dump() for item in payload.matchers]


def _out(session: Session, revision: NotificationPolicyRevision) -> PolicyOut:
    return PolicyOut.model_validate(policy_public_dict(session, revision))


@router.get("/notification-policies", response_model=list[PolicyOut])
def list_policies(session: Session = Depends(get_session)) -> list[PolicyOut]:
    revisions = session.exec(
        select(NotificationPolicyRevision).order_by(
            NotificationPolicyRevision.priority,
            NotificationPolicyRevision.logical_id,
            NotificationPolicyRevision.version.desc(),
        )
    ).all()
    return [_out(session, item) for item in revisions]


@router.post(
    "/notification-policies",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
)
def create_policy_endpoint(
    payload: PolicyDraftIn, session: Session = Depends(get_session)
) -> PolicyOut:
    try:
        revision = create_policy_draft(
            session,
            name=payload.name,
            priority=payload.priority,
            matchers=_matcher_dicts(payload),
            repeat_interval_seconds=payload.repeat_interval_seconds,
            channel_ids=payload.channel_ids,
            source_scope=payload.source_scope.model_dump(),
        )
        session.commit()
        session.refresh(revision)
        return _out(session, revision)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.put("/notification-policies/{logical_id}/draft", response_model=PolicyOut)
def update_policy_endpoint(
    logical_id: str,
    payload: PolicyDraftUpdate,
    session: Session = Depends(get_session),
) -> PolicyOut:
    try:
        revision = update_policy_draft(
            session,
            logical_id,
            expected_version=payload.expected_version,
            name=payload.name,
            priority=payload.priority,
            matchers=_matcher_dicts(payload),
            repeat_interval_seconds=payload.repeat_interval_seconds,
            channel_ids=payload.channel_ids,
            source_scope=payload.source_scope.model_dump(),
        )
        session.commit()
        session.refresh(revision)
        return _out(session, revision)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/notification-policies/preview", response_model=PolicyPreviewOut)
def preview_policy_endpoint(
    payload: PolicyPreviewIn,
    session: Session = Depends(get_session),
) -> PolicyPreviewOut:
    try:
        active = active_policy_candidates(session)
        next_id = max([item.id for item in active], default=0) + 1
        candidate = PolicyCandidate(
            id=next_id,
            logical_id=payload.logical_id or f"preview:{next_id}",
            name=payload.name,
            priority=payload.priority,
            matchers=validate_policy_matchers(_matcher_dicts(payload)),
            repeat_interval_seconds=validate_repeat_interval(
                payload.repeat_interval_seconds
            ),
            channel_ids=list(dict.fromkeys(payload.channel_ids)),
            scope_mode=payload.source_scope.mode,
            source_ids=tuple(payload.source_scope.source_ids),
        )
        incidents = session.exec(select(Incident)).all()
        disabled = {
            channel_id
            for channel_id in candidate.channel_ids
            if (
                (channel := session.get(NotificationChannel, channel_id)) is None
                or channel.state != "ENABLED"
                or channel.active_revision_id is None
            )
        }
        result = preview_policy(
            incidents,
            candidate=candidate,
            active=active,
            disabled_channel_ids=disabled,
        )
        return PolicyPreviewOut(
            direct_match_count=result.direct_match_count,
            shadowed_count=result.shadowed_count,
            final_match_count=result.final_match_count,
            firing_match_count=result.firing_match_count,
            unrouted_count=result.unrouted_count,
            disabled_channel_count=result.disabled_channel_count,
            samples=[item.__dict__ for item in result.samples],
            example_card={
                "title": f"{payload.name} · 通知示例",
                "severity": "critical",
                "channel_ids": payload.channel_ids,
                "preview_only": True,
            },
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post(
    "/notification-policies/{revision_id}/prepare-activation",
    response_model=PrepareActivationOut,
)
def prepare_activation_endpoint(
    revision_id: int,
    payload: PrepareActivationIn,
    session: Session = Depends(get_session),
) -> PrepareActivationOut:
    try:
        prepared = prepare_activation(
            session,
            revision_id,
            expected_version=payload.expected_version,
            notify_existing=payload.notify_existing,
            source_id=None,
        )
        return PrepareActivationOut(
            confirm_token=prepared.token,
            eligible_incident_count=prepared.eligible_incident_count,
            expires_at_epoch=prepared.expires_at_epoch,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post(
    "/notification-policies/{revision_id}/activate", response_model=PolicyOut
)
def confirm_activation_endpoint(
    revision_id: int,
    payload: ConfirmActivationIn,
    session: Session = Depends(get_session),
) -> PolicyOut:
    try:
        revision = confirm_activation(
            session,
            revision_id,
            expected_version=payload.expected_version,
            notify_existing=payload.notify_existing,
            token=payload.confirm_token,
            source_id=None,
        )
        session.commit()
        session.refresh(revision)
        return _out(session, revision)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc


@router.post("/notification-policies/{revision_id}/disable", response_model=PolicyOut)
def disable_policy_endpoint(
    revision_id: int, session: Session = Depends(get_session)
) -> PolicyOut:
    try:
        revision = disable_policy(session, revision_id)
        session.commit()
        session.refresh(revision)
        return _out(session, revision)
    except Exception as exc:
        session.rollback()
        raise _http_error(exc) from exc
