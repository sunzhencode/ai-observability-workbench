"""EventSource configuration service tests (offline and secret-safe).

F22 rewrote the contract these pin: a source owns one configuration row,
saving is applying, and a connection test is a diagnostic rather than a gate.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.crypto import SecretBox, SecretError
from app.registry_models import (
    AlertEndpointObservation,
    AlertmanagerEndpointRevision,
    EventSource,
    EventSourceRevision,
    F20Model,
    SourceThanosConfig,
)
from app.models import ConfigAudit, ConnectionProfile
from app.services.event_sources import (
    EndpointSnapshot,
    EndpointTestResult,
    EventSourceTestFailed,
    archive_event_source,
    create_event_source,
    disable_event_source,
    enable_event_source,
    event_source_public_dict,
    save_event_source_changes,
    test_event_source,
)
from app.services.event_sources import _config_row, _endpoints

BOX = SecretBox("event-source-test-key")


@pytest.fixture
def f20_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@dataclass
class StubEndpointTester:
    results: list[EndpointTestResult]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def test(self, endpoint, secret: str) -> EndpointTestResult:
        self.calls.append((endpoint.canonical_url, secret))
        return self.results.pop(0)


def _candidate(*, name: str = "Primary alerts", suffix: str = "a") -> dict:
    return {
        "name": name,
        "endpoints": [
            {
                "url": f"https://am-{suffix}-1.invalid",
                "enabled": True,
                "auth_type": "BEARER",
                "username": "",
                "secret_action": "REPLACE",
                "secret_value": f"sanitized-{suffix}-one",
            },
            {
                "url": f"https://am-{suffix}-2.invalid",
                "enabled": True,
                "auth_type": "BASIC",
                "username": "safe-user",
                "secret_action": "REPLACE",
                "secret_value": f"sanitized-{suffix}-two",
            },
        ],
        "poll_interval_seconds": 30,
        "resolution_grace_seconds": 60,
        "max_parallel_endpoints": 2,
        "watchdog_enabled": False,
        "watchdog_alertname": "Watchdog",
        "watchdog_identity_label": "cluster",
        "watchdog_missing_after_seconds": 90,
    }


def _minimal(*, name: str = "Just a name and an address") -> dict:
    """The whole minimal registration path: a name and an address."""
    return {"name": name, "endpoints": [{"url": "https://am-minimal.invalid"}]}


def test_a_name_and_an_address_are_enough_to_start_polling(f20_session) -> None:
    source = create_event_source(f20_session, candidate=_minimal())
    f20_session.commit()

    assert source.lifecycle_state == "ENABLED"
    assert source.active_revision_id is not None
    public = event_source_public_dict(f20_session, source)
    assert public["status"] == "ENABLED"
    assert public["config"]["poll_interval_seconds"] == 30
    assert public["config"]["endpoints"][0]["url"] == "https://am-minimal.invalid"
    assert public["config"]["thanos"] is None


def test_create_uses_stable_random_identity_and_encrypted_endpoints(
    f20_session,
) -> None:
    box = SecretBox("event-source-test-key")

    source = create_event_source(
        f20_session, candidate=_candidate(), enable=False, box=box
    )
    f20_session.commit()

    assert source.id.startswith("src_")
    assert "am-a" not in source.id
    assert source.lifecycle_state == "DISABLED"
    revision = f20_session.exec(select(EventSourceRevision)).one()
    assert revision.internal_state == "ACTIVE"
    endpoints = f20_session.exec(
        select(AlertmanagerEndpointRevision).order_by(
            AlertmanagerEndpointRevision.position
        )
    ).all()
    assert [item.position for item in endpoints] == [0, 1]
    assert box.decrypt(endpoints[0].secret_envelope_json) == "sanitized-a-one"
    rendered = repr(event_source_public_dict(f20_session, source))
    assert "sanitized-a-one" not in rendered
    assert "ciphertext" not in rendered
    assert "secret_envelope" not in rendered
    assert "internal_state" not in rendered
    assert "environment" not in rendered


def test_an_unreachable_address_still_registers(f20_session) -> None:
    """Deliberate F22 behaviour: the test button stopped being a gate.

    Requiring a passing test before a source could exist made the workbench
    unusable whenever the address was briefly unreachable, and turned the
    minimal path into four verbs.
    """
    source = create_event_source(
        f20_session, candidate=_minimal(name="Not reachable yet")
    )
    f20_session.commit()

    assert source.lifecycle_state == "ENABLED"
    assert f20_session.exec(select(EventSourceRevision)).one().last_test_status is None


def test_multiple_sources_can_be_enabled_without_connection_profile_constraint(
    f20_session,
) -> None:
    box = SecretBox("event-source-test-key")
    for name, suffix in (("Source one", "one"), ("Source two", "two")):
        source = create_event_source(
            f20_session,
            candidate=_candidate(name=name, suffix=suffix),
            box=box,
        )
        assert source.lifecycle_state == "ENABLED"
        assert source.active_revision_id is not None
    f20_session.commit()

    assert len(
        f20_session.exec(
            select(EventSource).where(EventSource.lifecycle_state == "ENABLED")
        ).all()
    ) == 2
    assert f20_session.exec(select(ConnectionProfile)).all() == []


def test_watchdog_missing_after_defaults_from_poll_interval_and_validates_floor(
    f20_session,
) -> None:
    candidate = _candidate()
    candidate["poll_interval_seconds"] = 120
    candidate.pop("watchdog_missing_after_seconds")

    create_event_source(
        f20_session,
        candidate=candidate,
        enable=False,
        box=SecretBox("event-source-test-key"),
    )
    revision = f20_session.exec(select(EventSourceRevision)).one()
    assert revision.watchdog_missing_after_seconds == 360

    invalid = _candidate(name="Invalid floor", suffix="floor")
    invalid["poll_interval_seconds"] = 120
    invalid["watchdog_missing_after_seconds"] = 60
    with pytest.raises(ValueError, match="at least poll interval"):
        create_event_source(
            f20_session,
            candidate=invalid,
            enable=False,
            box=SecretBox("event-source-test-key"),
        )


def test_none_auth_rejects_unused_secret_instead_of_persisting_it(
    f20_session,
) -> None:
    candidate = _candidate()
    candidate["endpoints"][0].update(
        {
            "auth_type": "NONE",
            "secret_action": "REPLACE",
            "secret_value": "unused-secret-must-not-be-stored",
        }
    )

    with pytest.raises(ValueError, match="NONE auth"):
        create_event_source(
            f20_session,
            candidate=candidate,
            enable=False,
            box=SecretBox("event-source-test-key"),
        )

    assert f20_session.exec(select(EventSource)).all() == []


def test_saving_is_applying_and_leaves_one_configuration_row(f20_session) -> None:
    box = SecretBox("event-source-test-key")
    source = create_event_source(f20_session, candidate=_candidate(), box=box)
    original_revision = source.active_revision_id

    save_event_source_changes(
        f20_session,
        source.id,
        candidate=_candidate(name="Renamed source", suffix="changed"),
        expected_version=1,
        box=box,
    )
    f20_session.commit()

    rows = f20_session.exec(
        select(EventSourceRevision).where(
            EventSourceRevision.source_id == source.id
        )
    ).all()
    assert [item.internal_state for item in rows] == ["ACTIVE"]
    stored = f20_session.get(EventSource, source.id)
    assert stored.name == "Renamed source"
    assert stored.active_revision_id == original_revision
    public = event_source_public_dict(f20_session, stored)
    assert public["config"]["endpoints"][0]["url"] == "https://am-changed-1.invalid"
    assert [item.action for item in f20_session.exec(select(ConfigAudit)).all()] == [
        "CREATE",
        "SAVE",
    ]


def test_removing_an_endpoint_takes_its_observations_with_it(f20_session) -> None:
    """A removed endpoint row cannot be left with references pointing at it."""
    box = SecretBox("event-source-test-key")
    source = create_event_source(f20_session, candidate=_candidate(), box=box)
    f20_session.flush()
    second = f20_session.exec(
        select(AlertmanagerEndpointRevision).where(
            AlertmanagerEndpointRevision.position == 1
        )
    ).one()
    f20_session.add(
        AlertEndpointObservation(alert_id=1, endpoint_revision_id=int(second.id))
    )
    f20_session.flush()

    shrunk = _candidate()
    shrunk["endpoints"] = shrunk["endpoints"][:1]
    save_event_source_changes(
        f20_session, source.id, candidate=shrunk, expected_version=1, box=box
    )
    f20_session.commit()

    assert [
        item.position
        for item in f20_session.exec(select(AlertmanagerEndpointRevision)).all()
    ] == [0]
    assert f20_session.exec(select(AlertEndpointObservation)).all() == []


@pytest.mark.asyncio
async def test_a_failed_test_reports_the_source_without_undoing_the_save(
    f20_session,
) -> None:
    box = SecretBox("event-source-test-key")
    source = create_event_source(f20_session, candidate=_candidate(), box=box)

    failed = await test_event_source(
        f20_session,
        source.id,
        box=box,
        tester=StubEndpointTester(
            [
                EndpointTestResult(True, "OK"),
                EndpointTestResult(False, "ENDPOINT_HTTP_5XX"),
            ]
        ),
    )
    f20_session.commit()

    assert failed.ok is False
    stored = f20_session.get(EventSource, source.id)
    assert stored.lifecycle_state == "ENABLED"
    public = event_source_public_dict(f20_session, stored)
    assert public["status"] == "CONNECTION_ERROR"
    assert public["config"]["safe_error_code"] == "ENDPOINT_TEST_FAILED"


def test_optimistic_lock_casefold_name_and_lifecycle_guards(f20_session) -> None:
    box = SecretBox("event-source-test-key")
    first = create_event_source(
        f20_session, candidate=_candidate(), enable=False, box=box
    )
    with pytest.raises(ValueError, match="name"):
        create_event_source(
            f20_session,
            candidate=_candidate(name=" primary ALERTS ", suffix="other"),
            enable=False,
            box=box,
        )
    with pytest.raises(FileExistsError, match="conflict"):
        save_event_source_changes(
            f20_session,
            first.id,
            candidate=_candidate(name="Changed"),
            expected_version=99,
            box=box,
        )

    enabled = enable_event_source(f20_session, first.id, expected_version=1)
    assert enabled.lifecycle_state == "ENABLED"
    disabled = disable_event_source(f20_session, first.id, expected_version=2)
    assert disabled.lifecycle_state == "DISABLED"
    archived = archive_event_source(f20_session, first.id, expected_version=3)
    assert archived.lifecycle_state == "ARCHIVED"
    assert f20_session.get(EventSource, first.id) is not None
    with pytest.raises(ValueError, match="archived"):
        enable_event_source(f20_session, first.id, expected_version=4)


def test_enabling_without_any_configuration_is_refused(f20_session) -> None:
    source = EventSource(id="src_empty", name="No configuration")
    f20_session.add(source)
    f20_session.flush()

    with pytest.raises(EventSourceTestFailed) as raised:
        enable_event_source(f20_session, source.id, expected_version=1)
    assert raised.value.result.code == "NO_CONFIGURATION"


@pytest.mark.asyncio
async def test_wrong_key_fails_closed_without_changing_test_state(f20_session) -> None:
    source = create_event_source(
        f20_session,
        candidate=_candidate(),
        enable=False,
        box=SecretBox("correct-key"),
    )
    revision = f20_session.exec(select(EventSourceRevision)).one()

    with pytest.raises(SecretError):
        await test_event_source(
            f20_session,
            source.id,
            box=SecretBox("wrong-key"),
            tester=StubEndpointTester([]),
        )

    f20_session.refresh(revision)
    assert revision.last_test_status is None


@pytest.mark.asyncio
async def test_single_endpoint_test_is_audited_as_partial(f20_session) -> None:
    box = SecretBox("event-source-test-key")
    source = create_event_source(
        f20_session, candidate=_candidate(), enable=False, box=box
    )

    result = await test_event_source(
        f20_session,
        source.id,
        positions={0},
        box=box,
        tester=StubEndpointTester([EndpointTestResult(True, "OK")]),
    )

    assert result.ok is True
    revision = f20_session.exec(select(EventSourceRevision)).one()
    assert revision.last_test_status == "PARTIAL"
    public = event_source_public_dict(f20_session, source)
    assert public["config"]["endpoints"][0]["last_test"]["code"] == "OK"
    assert public["config"]["endpoints"][1]["last_test"] is None


@pytest.mark.asyncio
async def test_request_candidate_test_is_read_only(f20_session) -> None:
    box = SecretBox("event-source-test-key")
    source = create_event_source(
        f20_session, candidate=_candidate(), enable=False, box=box
    )
    request_candidate = _candidate(name="Request only", suffix="request")

    result = await test_event_source(
        f20_session,
        source.id,
        candidate=request_candidate,
        box=box,
        tester=StubEndpointTester(
            [EndpointTestResult(True, "OK"), EndpointTestResult(True, "OK")]
        ),
    )

    assert result.ok is True
    revision = f20_session.exec(select(EventSourceRevision)).one()
    assert revision.last_test_status is None
    stored_urls = [
        item.canonical_url
        for item in f20_session.exec(
            select(AlertmanagerEndpointRevision).order_by(
                AlertmanagerEndpointRevision.position
            )
        ).all()
    ]
    assert stored_urls == [
        "https://am-a-1.invalid",
        "https://am-a-2.invalid",
    ]


def test_history_address_is_a_field_on_the_source(f20_session) -> None:
    box = SecretBox("event-source-test-key")
    candidate = _minimal(name="With history")
    candidate["thanos"] = {
        "url": "https://thanos.invalid",
        "auth_type": "BEARER",
        "secret_action": "REPLACE",
        "secret_value": "thanos-token-must-not-leak",
        "timeout_seconds": 20,
    }

    source = create_event_source(f20_session, candidate=candidate, box=box)
    f20_session.commit()

    stored = f20_session.exec(select(SourceThanosConfig)).one()
    assert stored.source_id == source.id
    assert box.decrypt(stored.secret_envelope_json) == "thanos-token-must-not-leak"
    public = event_source_public_dict(f20_session, source)
    assert public["config"]["thanos"]["url"] == "https://thanos.invalid"
    assert public["config"]["thanos"]["secret_configured"] is True
    assert "thanos-token-must-not-leak" not in repr(public)


def test_clearing_the_history_address_removes_it(f20_session) -> None:
    box = SecretBox("event-source-test-key")
    candidate = _minimal(name="History then none")
    candidate["thanos"] = {"url": "https://thanos.invalid"}
    source = create_event_source(f20_session, candidate=candidate, box=box)

    save_event_source_changes(
        f20_session,
        source.id,
        candidate=_minimal(name="History then none"),
        expected_version=1,
        box=box,
    )
    f20_session.commit()

    assert f20_session.exec(select(SourceThanosConfig)).all() == []
    public = event_source_public_dict(f20_session, source)
    assert public["config"]["thanos"] is None


def test_optimistic_lock_is_atomic_across_database_sessions(tmp_path) -> None:
    database_path = tmp_path / "event-source-lock.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    box = SecretBox("event-source-test-key")
    with Session(engine) as setup:
        source = create_event_source(
            setup, candidate=_candidate(), enable=False, box=box
        )
        setup.commit()
        source_id = source.id

    with Session(engine) as first, Session(engine) as stale:
        assert first.get(EventSource, source_id).version == 1
        assert stale.get(EventSource, source_id).version == 1
        save_event_source_changes(
            first,
            source_id,
            candidate=_candidate(name="First writer"),
            expected_version=1,
            box=box,
        )
        first.commit()

        with pytest.raises(FileExistsError, match="conflict"):
            save_event_source_changes(
                stale,
                source_id,
                candidate=_candidate(name="Stale writer", suffix="stale"),
                expected_version=1,
                box=box,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, [], EndpointTestResult(True, "OK")),
        (401, [], EndpointTestResult(False, "ENDPOINT_HTTP_4XX")),
        (503, [], EndpointTestResult(False, "ENDPOINT_HTTP_5XX")),
        (200, {"unexpected": True}, EndpointTestResult(False, "ENDPOINT_PARSE")),
    ],
)
async def test_http_endpoint_tester_uses_bounded_read_only_get_and_safe_codes(
    status_code, payload, expected
) -> None:
    import httpx
    from app.services.event_sources import HTTPAlertmanagerEndpointTester

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_code, json=payload)

    endpoint = EndpointSnapshot(
        position=0,
        canonical_url="https://read-only.invalid",
        enabled=True,
        auth_type="NONE",
        username="",
        secret_configured=False,
    )

    result = await HTTPAlertmanagerEndpointTester(
        transport=httpx.MockTransport(handler)
    ).test(endpoint, "")

    assert result == expected
    assert calls[0].method == "GET"
    assert calls[0].url.path == "/api/v2/alerts"
    assert dict(calls[0].url.params) == {
        "active": "true",
        "silenced": "false",
        "inhibited": "false",
    }


@pytest.mark.asyncio
async def test_http_endpoint_tester_stops_at_response_limit() -> None:
    import httpx
    from app.services.event_sources import HTTPAlertmanagerEndpointTester

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
    )
    endpoint = EndpointSnapshot(
        position=0,
        canonical_url="https://bounded.invalid",
        enabled=True,
        auth_type="NONE",
        username="",
        secret_configured=False,
    )

    result = await HTTPAlertmanagerEndpointTester(transport=transport).test(
        endpoint, ""
    )

    assert result == EndpointTestResult(False, "ENDPOINT_RESPONSE_TOO_LARGE")


def test_removing_a_middle_endpoint_keeps_each_url_with_its_own_secret(f20_session):
    """A position owns a credential, so it cannot be inferred from array order.

    The UI's 「移除」 filters by array index. Before endpoints carried their slot,
    every endpoint after the removed one shifted down and its `KEEP` secret
    resolved to the previous occupant's -- pairing a URL with another
    endpoint's token, silently.
    """
    def endpoint(position, url, token):
        return {
            "position": position,
            "url": url,
            "enabled": True,
            "auth_type": "BEARER",
            "username": "",
            "secret_action": "REPLACE",
            "secret_value": token,
        }

    source = create_event_source(
        f20_session,
        candidate={
            "name": "ha",
            "endpoints": [
                endpoint(0, "https://am-0.test", "token-ZERO"),
                endpoint(1, "https://am-1.test", "token-ONE"),
                endpoint(2, "https://am-2.test", "token-TWO"),
            ],
        },
        box=BOX,
    )
    f20_session.commit()

    def keep(position, url):
        return {
            "position": position,
            "url": url,
            "enabled": True,
            "auth_type": "BEARER",
            "username": "",
            "secret_action": "KEEP",
            "secret_value": None,
        }

    save_event_source_changes(
        f20_session,
        source.id,
        candidate={
            "name": "ha",
            "endpoints": [
                keep(0, "https://am-0.test"),
                keep(2, "https://am-2.test"),
            ],
        },
        expected_version=source.version,
        box=BOX,
    )
    f20_session.commit()

    stored = {
        row.canonical_url: BOX.decrypt(row.secret_envelope_json)
        for row in _endpoints(f20_session, _config_row(f20_session, source.id).id)
    }
    assert stored == {
        "https://am-0.test": "token-ZERO",
        "https://am-2.test": "token-TWO",
    }


def test_positions_must_be_unique_and_all_or_nothing(f20_session):
    def endpoint(position, url):
        return {
            "position": position,
            "url": url,
            "enabled": True,
            "auth_type": "NONE",
            "username": "",
            "secret_action": "CLEAR",
            "secret_value": None,
        }

    with pytest.raises(ValueError, match="unique"):
        create_event_source(
            f20_session,
            candidate={
                "name": "dupe",
                "endpoints": [
                    endpoint(0, "https://a.test"),
                    endpoint(0, "https://b.test"),
                ],
            },
            box=BOX,
        )

    with pytest.raises(ValueError, match="every endpoint or none"):
        create_event_source(
            f20_session,
            candidate={
                "name": "partial",
                "endpoints": [
                    endpoint(0, "https://a.test"),
                    {**endpoint(1, "https://b.test"), "position": None},
                ],
            },
            box=BOX,
        )
