"""Defects found by reasoning about F27 as a whole, not by a failing test.

Each of these is a place where two parts were individually correct and wrong
together, or where a requirement was written down and then only half built.
They are grouped here because they were found in one pass; each has its fix in
the module it belongs to.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.crypto import SecretBox
from app.registry_models import F20Model, ModelCallLog, ModelChannel
from app.services.model_channels import (
    activate_model_channel_revision,
    create_model_channel,
    resolve_active_model,
)

BOX = SecretBox("f27-logic-review-key")


@pytest.fixture()
def client(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import metrics
    from app.config_schemas import safe_validation_exception_handler
    from app.db import get_session
    from fastapi.exceptions import RequestValidationError

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, safe_validation_exception_handler)
    app.include_router(metrics.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _activated(session: Session, name: str) -> ModelChannel:
    channel, revision = create_model_channel(
        session,
        name=name,
        kind="OPENAI_COMPATIBLE",
        base_url="https://api.example.com/v1",
        model=name,
        api_key_action="REPLACE",
        api_key_value="offline-key",
        box=BOX,
    )
    revision.tested_ok_at = datetime.now(timezone.utc)
    session.add(revision)
    session.flush()
    activate_model_channel_revision(session, revision.id, box=BOX)
    session.flush()
    return channel


# --- 1. two enabled channels, one silently wins ----------------------------


def test_activating_a_second_channel_retires_the_first(session) -> None:
    """Otherwise the user configures a new service and nothing changes.

    `resolve_active_model` returns exactly one channel — it takes the lowest id
    when several are enabled. So enabling a second one looked like it worked,
    the UI showed both as 已启用, and every call kept going to the first. There
    is no screen anywhere that would tell you which one is actually in use.

    A single-user workbench has one model service. Making that true in the data
    beats explaining it in a tooltip.
    """
    first = _activated(session, "first")
    second = _activated(session, "second")

    enabled = session.exec(select(ModelChannel).where(ModelChannel.enabled == True)).all()  # noqa: E712
    assert [item.name for item in enabled] == ["second"], (
        "两个通道同时启用，而调用只会走其中一个——用户无从知道是哪个"
    )
    assert first.enabled is False and second.enabled is True

    active = resolve_active_model(session, box=BOX)
    assert active is not None and active.name == "second"


# --- 2. a template with an unsafe range is only refused when charted -------


def test_a_template_with_an_unbounded_range_is_refused_at_save(client) -> None:
    """D37 says user templates are validated **when saved**, not when charted.

    Saving `rate(x[30d])` succeeded and then failed silently at chart time as
    one red line among others, which is exactly the "learn about it later, in
    the wrong place" the requirement exists to prevent.
    """
    response = client.post(
        "/api/metric-templates",
        json={
            "name": "扫三十天",
            "promql": "rate(node_network_receive_bytes_total[30d])",
            "required_labels": [],
            "description": "",
        },
    )
    assert response.status_code == 422, response.text
    assert "30d" not in response.text, "报错把用户输入原样带出去了"


def test_an_edit_cannot_smuggle_an_unsafe_range_past_the_save_check(client) -> None:
    created = client.post(
        "/api/metric-templates",
        json={
            "name": "起初安全",
            "promql": "node_load1",
            "required_labels": [],
            "description": "",
        },
    ).json()
    response = client.patch(
        f"/api/metric-templates/{created['id']}",
        json={"promql": "rate(node_load1[30d])"},
    )
    assert response.status_code == 422, "保存时校验只挡住了新建，编辑是条绕路"


# --- 3. the call log grows forever ----------------------------------------


def test_the_model_call_log_is_covered_by_retention(session) -> None:
    """A table nothing ever deletes from is a slow leak in a local SQLite file.

    Every other运行 table here has a retention window; this one was added for
    audit and idempotency and then left out of the cleanup, so it would grow
    for the life of the install.
    """
    from app.services.retention import cleanup_expired_data

    old = ModelCallLog(
        purpose="SUGGEST_QUERIES",
        alert_id=1,
        created_at=datetime.now(timezone.utc) - timedelta(days=200),
    )
    recent = ModelCallLog(purpose="SUGGEST_QUERIES", alert_id=2)
    session.add(old)
    session.add(recent)
    session.commit()

    cleanup_expired_data(session, now=datetime.now(timezone.utc), retention_days=30)

    rows = session.exec(select(ModelCallLog)).all()
    assert [row.alert_id for row in rows] == [2], "调查记录保留期没有覆盖这张表"
