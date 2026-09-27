"""Model channels: atomic activation, encrypted secrets, and diagnostics.

Every call is offline.  ``FAKE`` mode is the same registry gate used by
``start.sh --mock``; the database is in memory and the secret values below are
deliberately non-key-shaped test strings.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import model_channels
from app.config import settings
from app.config_schemas import safe_validation_exception_handler
from app.crypto import SecretBox
from app.db import get_session
from app.registry_models import F20Model, ModelChannelRevision
from app.models import ConfigAudit
from app.providers.model.base import ModelReply
from app.providers.model.fake import FakeModelClient
from app.services.model_channels import (
    SYNTHETIC_FACT_ID,
    SYNTHETIC_HYPOTHESIS,
    create_model_channel,
    test_model_channel_revision,
    update_model_channel_draft,
)
from app.services.investigation_contract import structured_response_format

PUBLIC_URL = "https://model.example.com/v1"
TEST_SECRET = "offline-model-secret"


@pytest.fixture()
def model_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


@pytest.fixture(autouse=True)
def _fake_model_mode(monkeypatch):
    monkeypatch.setattr(settings, "model_provider_mode", "FAKE")
    monkeypatch.setattr(settings, "model_fake_script", "OK")


def _client(session: Session) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError, safe_validation_exception_handler
    )
    app.include_router(model_channels.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _create(client: TestClient, *, name: str = "primary-model"):
    return client.post(
        "/api/model-channels",
        json={
            "name": name,
            "config": {
                "kind": "OPENAI_COMPATIBLE",
                "base_url": PUBLIC_URL,
                "model": "offline-model",
                "api_key": {"action": "REPLACE", "value": TEST_SECRET},
            },
        },
    )


def _valid_reply() -> ModelReply:
    return ModelReply(
        text=json.dumps(
            {
                "hypotheses": [
                    {
                        "statement": SYNTHETIC_HYPOTHESIS,
                        "verdict": "SUPPORTED",
                        "supporting_fact_ids": [SYNTHETIC_FACT_ID],
                        "contradicting_fact_ids": [],
                        "missing_evidence": [],
                        "recommendations": [],
                    }
                ]
            }
        ),
        model_name="fake-contract-model",
    )


def test_structured_schema_requires_every_declared_hypothesis_field() -> None:
    schema = structured_response_format()["json_schema"]["schema"]
    hypothesis = schema["$defs"]["Hypothesis"]
    assert set(hypothesis["required"]) == set(hypothesis["properties"])
    assert hypothesis["additionalProperties"] is False


def test_create_encrypts_secret_and_api_never_echoes_it(model_session) -> None:
    client = _client(model_session)
    response = _create(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "OPENAI_COMPATIBLE"
    assert body["enabled"] is True
    assert body["active_revision_id"] == body["revisions"][0]["id"]
    assert body["revisions"][0] == {
        **body["revisions"][0],
        "state": "ACTIVE",
        "base_url": PUBLIC_URL,
        "base_host": "model.example.com",
        "model": "offline-model",
        "secret_configured": True,
        "tested_ok_at": None,
    }
    assert TEST_SECRET not in response.text

    stored = model_session.exec(select(ModelChannelRevision)).one()
    envelope = json.loads(stored.secret_envelope_json)
    assert envelope["ciphertext"]
    assert TEST_SECRET not in stored.secret_envelope_json
    assert SecretBox(settings.master_key).decrypt(envelope) == TEST_SECRET


def test_validation_failure_does_not_echo_secret_or_private_address(
    model_session,
) -> None:
    response = _client(model_session).post(
        "/api/model-channels",
        json={
            "name": "bad",
            "config": {
                "kind": "OPENAI_COMPATIBLE",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "x",
                "api_key": {"action": "REPLACE", "value": TEST_SECRET},
            },
        },
    )

    assert response.status_code == 422
    assert TEST_SECRET not in response.text
    assert "127.0.0.1" not in response.text


def test_kind_and_name_are_immutable_and_unknown_kind_is_rejected(
    model_session,
) -> None:
    client = _client(model_session)
    created = _create(client).json()
    revision_id = created["revisions"][0]["id"]

    unknown = client.post(
        "/api/model-channels",
        json={
            "name": "unknown",
            "config": {
                "kind": "LOCAL_OPENAI_COMPATIBLE",
                "base_url": PUBLIC_URL,
                "model": "x",
                "api_key": {"action": "REPLACE", "value": "x"},
            },
        },
    )
    assert unknown.status_code == 422

    changed = client.put(
        f"/api/model-channels/{created['id']}/draft",
        json={
            "expected_revision_id": revision_id,
            "config": {
                "kind": "LOCAL_OPENAI_COMPATIBLE",
                "base_url": PUBLIC_URL,
                "model": "x",
                "api_key": {"action": "KEEP"},
            },
        },
    )
    assert changed.status_code == 422


@pytest.mark.asyncio
async def test_secret_keep_preserves_write_only_value_and_edit_activates_new_revision(
    model_session,
) -> None:
    box = SecretBox("model-channel-test-key")
    channel, draft = create_model_channel(
        model_session,
        name="model",
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="one",
        api_key_action="REPLACE",
        api_key_value=TEST_SECRET,
        box=box,
    )
    result = await test_model_channel_revision(
        model_session, draft.id, box=box, client=FakeModelClient([_valid_reply()])
    )
    assert result.ok is True
    assert draft.tested_ok_at is not None

    updated = update_model_channel_draft(
        model_session,
        channel.id,
        expected_revision_id=draft.id,
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="two",
        api_key_action="KEEP",
        api_key_value=None,
        box=box,
    )
    assert updated.id != draft.id
    assert draft.state == "RETIRED"
    assert updated.state == "ACTIVE"
    assert updated.tested_ok_at is None
    assert SecretBox("model-channel-test-key").decrypt(
        json.loads(updated.secret_envelope_json)
    ) == TEST_SECRET


@pytest.mark.asyncio
async def test_channel_test_uses_full_synthetic_contract_and_fact_validation(
    model_session,
) -> None:
    box = SecretBox("model-channel-test-key")
    _channel, draft = create_model_channel(
        model_session,
        name="model",
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="one",
        api_key_action="REPLACE",
        api_key_value=TEST_SECRET,
        box=box,
    )
    fake = FakeModelClient([_valid_reply()])

    result = await test_model_channel_revision(
        model_session, draft.id, box=box, client=fake
    )

    assert result.ok is True
    assert result.code == "OK"
    assert result.possibly_billed is True
    assert draft.tested_ok_at is not None
    call = fake.calls[0]
    serialized = json.dumps(call.messages, ensure_ascii=False)
    assert SYNTHETIC_FACT_ID in serialized
    assert "raw_payload" not in serialized
    assert "generatorURL" not in serialized
    assert call.tools == ()
    assert call.response_format


@pytest.mark.asyncio
async def test_invalid_test_result_does_not_disable_or_rollback_active_revision(
    model_session,
) -> None:
    box = SecretBox("model-channel-test-key")
    channel, draft = create_model_channel(
        model_session,
        name="model",
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="one",
        api_key_action="REPLACE",
        api_key_value=TEST_SECRET,
        box=box,
    )
    for text in (
        "ok",
        _valid_reply().text.replace(SYNTHETIC_FACT_ID, "fabricated_fact"),
    ):
        result = await test_model_channel_revision(
            model_session,
            draft.id,
            box=box,
            client=FakeModelClient([ModelReply(text=text)]),
        )
        assert result.ok is False
        assert result.code == "CONTRACT_INVALID"
        assert draft.tested_ok_at is None
        assert draft.state == "ACTIVE"
        assert channel.enabled is True


@pytest.mark.asyncio
async def test_save_retires_previous_active_revision_without_a_test_gate(model_session) -> None:
    box = SecretBox("model-channel-test-key")
    channel, first = create_model_channel(
        model_session,
        name="model",
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="one",
        api_key_action="REPLACE",
        api_key_value=TEST_SECRET,
        box=box,
    )
    second = update_model_channel_draft(
        model_session,
        channel.id,
        expected_revision_id=first.id,
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="two",
        api_key_action="KEEP",
        api_key_value=None,
        box=box,
    )
    assert second.id != first.id
    assert first.state == "RETIRED"
    assert second.state == "ACTIVE"
    assert channel.enabled is True


def test_optional_test_is_audited_without_changing_activation(model_session) -> None:
    response = _create(_client(model_session))
    revision_id = response.json()["revisions"][0]["id"]
    client = _client(model_session)

    tested = client.post(f"/api/model-channel-revisions/{revision_id}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json() == {
        "ok": True,
        "code": "OK",
        "possibly_billed": True,
        "detail": "",
    }
    refreshed = client.get("/api/model-channels").json()[0]
    assert refreshed["enabled"] is True
    assert refreshed["revisions"][0]["state"] == "ACTIVE"

    actions = model_session.exec(
        select(ConfigAudit.action)
        .where(ConfigAudit.resource_type == "MODEL_CHANNEL")
        .order_by(ConfigAudit.id)
    ).all()
    assert actions == ["CREATE_DRAFT", "ACTIVATE", "TEST"]


def test_source_does_not_return_secret_envelope_or_accept_a_disable_switch() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "app"
        / "api"
        / "model_channels.py"
    ).read_text(encoding="utf-8")
    assert "secret_envelope_json" not in source
    assert "disable_egress" not in source


def test_the_fake_ok_reply_still_satisfies_the_real_contract() -> None:
    """Guards a deliberate duplication.

    `providers/model/fake.py` spells the synthetic answer out as a literal
    because `providers/` must not import `services/`. That is fine only while
    something checks the two have not drifted -- otherwise a contract change
    would make every channel test fail under `--mock`, and the cause would be
    invisible.
    """
    from app.providers.model.fake import _SYNTHETIC_CONTRACT_REPLY
    from app.services.investigation_contract import validate_investigation_result

    result = validate_investigation_result(
        json.dumps(_SYNTHETIC_CONTRACT_REPLY),
        available_fact_ids={SYNTHETIC_FACT_ID},
    )
    hypothesis = result.hypotheses[0]
    assert hypothesis.statement == SYNTHETIC_HYPOTHESIS
    assert hypothesis.verdict == "SUPPORTED"
    assert hypothesis.supporting_fact_ids == [SYNTHETIC_FACT_ID]


def test_a_failed_test_says_which_kind_of_failure_it_was() -> None:
    """The stage-1 defect, repeated and now guarded.

    `bd21c07` fixed exactly this once already: a whole taxonomy of failure codes
    was computed and then the one field that distinguishes them was not
    rendered, so every upstream problem read as one undifferentiated error.
    "Wrong model name" (404) and "wrong key" (401) send the reader to completely
    different places; collapsing them wastes the classification entirely.
    """
    from app.providers.model.base import ModelCallError, ModelFailureKind
    from app.services.model_channels import ModelChannelTestResult

    result = ModelChannelTestResult(
        ok=False,
        code=ModelFailureKind.SERVICE_ERROR.value,
        possibly_billed=True,
        detail="HTTP_404",
    )
    assert result.detail == "HTTP_404"
    # And the error type it comes from must actually carry it.
    assert ModelCallError(ModelFailureKind.SERVICE_ERROR, "HTTP_404").detail == "HTTP_404"


@pytest.mark.asyncio
async def test_listing_models_reports_a_failure_instead_of_an_empty_picker(
    model_session,
) -> None:
    """An empty dropdown and "your key is wrong" must not look the same."""
    from app.providers.model.base import ModelCallError, ModelFailureKind
    from app.services.model_channels import list_revision_models

    box = SecretBox("model-channel-test-key")
    _channel, draft = create_model_channel(
        model_session,
        name="model",
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="one",
        api_key_action="REPLACE",
        api_key_value=TEST_SECRET,
        box=box,
    )

    async def failing(_config):
        raise ModelCallError(ModelFailureKind.AUTH_FAILED, "HTTP_401")

    result = await list_revision_models(
        model_session, draft.id, box=box, lister=failing
    )
    assert result.ok is False
    assert (result.code, result.detail) == ("AUTH_FAILED", "HTTP_401")
    assert result.models is None


@pytest.mark.asyncio
async def test_listing_models_returns_what_the_service_offers(model_session) -> None:
    from app.services.model_channels import list_revision_models

    box = SecretBox("model-channel-test-key")
    _channel, draft = create_model_channel(
        model_session,
        name="model",
        kind="OPENAI_COMPATIBLE",
        base_url=PUBLIC_URL,
        model="one",
        api_key_action="REPLACE",
        api_key_value=TEST_SECRET,
        box=box,
    )

    seen = {}

    async def lister(config):
        # The key reaches the service from the encrypted revision, never from
        # the browser: the picker button carries no credential.
        seen["api_key"] = config.api_key
        return ["gpt-4o", "gpt-4o-mini"]

    result = await list_revision_models(
        model_session, draft.id, box=box, lister=lister
    )
    assert result.ok is True
    assert result.models == ["gpt-4o", "gpt-4o-mini"]
    assert seen["api_key"] == TEST_SECRET


def test_every_vendor_preset_would_survive_the_egress_guard() -> None:
    """A preset the guard rejects has no business being offered.

    Shape only — resolving DNS here would make the test suite reach the network
    and fail when someone is offline. The shape check is what catches the
    mistake that actually happens: a preset written without `https`, with a
    stray port, or with credentials in the URL.
    """
    from app.providers.egress import assert_https_shape
    from app.providers.model.vendors import CUSTOM_VENDOR, MODEL_VENDORS

    for vendor in MODEL_VENDORS:
        if vendor.id == CUSTOM_VENDOR:
            assert vendor.base_url == "", "自定义项不该预置地址"
            continue
        host, port = assert_https_shape(vendor.base_url)
        assert host and port == 443


def test_a_preset_ignores_a_stale_custom_url(session=None) -> None:
    """Choosing a preset after typing a custom address must not keep the typing.

    Otherwise the form shows "OpenAI" and the request goes somewhere else — the
    worst kind of wrong, because the screen says the right thing.
    """
    from app.providers.model.vendors import base_url_for

    assert base_url_for("OPENAI", "https://stale.example/v1") == (
        "https://api.openai.com/v1"
    )
    assert base_url_for("CUSTOM", "https://my.gateway/v1") == "https://my.gateway/v1"


def test_vendor_presets_offer_real_models_before_channel_save(model_session) -> None:
    vendors = _client(model_session).get("/api/model-vendors")

    assert vendors.status_code == 200
    by_id = {item["id"]: item for item in vendors.json()}
    assert by_id["OPENAI"]["recommended_models"] == ["gpt-5.5"]
    assert by_id["DEEPSEEK"]["recommended_models"] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]
    assert by_id["DASHSCOPE"]["recommended_models"] == [
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen3.7-flash",
    ]
    assert by_id["ZHIPU"]["recommended_models"] == ["glm-5.2"]
    assert by_id["MOONSHOT"]["recommended_models"] == [
        "kimi-k2.6",
        "kimi-k2.5",
    ]
    assert by_id["CUSTOM"]["recommended_models"] == []
    assert all(
        name not in {"fake-planner", "fake-model"}
        for vendor in by_id.values()
        for name in vendor["recommended_models"]
    )


def test_save_and_enable_requires_a_model_name(model_session) -> None:
    client = _client(model_session)
    response = client.post(
        "/api/model-channels",
        json={
            "name": "openai",
            "config": {
                "kind": "OPENAI_COMPATIBLE",
                "base_url": PUBLIC_URL,
                "model": "",
                "api_key": {"action": "REPLACE", "value": TEST_SECRET},
            },
        },
    )
    assert response.status_code == 422


def test_service_rejects_save_and_enable_without_a_model(model_session) -> None:
    box = SecretBox("model-channel-test-key")
    with pytest.raises(ValueError, match="model name"):
        create_model_channel(
            model_session,
            name="model",
            kind="OPENAI_COMPATIBLE",
            base_url=PUBLIC_URL,
            model="",
            api_key_action="REPLACE",
            api_key_value=TEST_SECRET,
            box=box,
        )


def test_mock_mode_is_visible_rather_than_silent(model_session, monkeypatch) -> None:
    """The screen must not read 已启用 while a fake client answers.

    `--mock` substituting a fake for every kind is correct: a mock run must not
    reach a real service. Hiding that substitution is not — a user configures a
    real channel, sees it enabled, and gets answers from something that never
    left the machine.
    """
    client = _client(model_session)

    monkeypatch.setattr(settings, "model_provider_mode", "FAKE")
    assert client.get("/api/model-runtime").json() == {"fake_mode": True}

    monkeypatch.setattr(settings, "model_provider_mode", "REAL")
    assert client.get("/api/model-runtime").json() == {"fake_mode": False}
