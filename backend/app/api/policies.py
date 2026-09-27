"""Local grouping-policy read/update API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.db import get_session
from app.schemas import GroupingPolicyOut, GroupingPolicyPut
from app.services.policies import get_active_policy, update_policy_and_regroup

router = APIRouter()


def _out(policy) -> GroupingPolicyOut:
    return GroupingPolicyOut.model_validate(policy.model_dump())


@router.get("/policies", response_model=GroupingPolicyOut)
def get_policy(session: Session = Depends(get_session)) -> GroupingPolicyOut:
    return _out(get_active_policy(session))


@router.put("/policies", response_model=GroupingPolicyOut)
def put_policy(
    payload: GroupingPolicyPut,
    session: Session = Depends(get_session),
) -> GroupingPolicyOut:
    return _out(update_policy_and_regroup(session, payload.group_by))
