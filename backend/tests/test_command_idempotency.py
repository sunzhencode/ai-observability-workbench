"""Durable at-most-once contracts for candidate resource creation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest

from app.adapters.persistence.commands import SqlAlchemyCommandReceiptStore
from app.application.commands import CommandConflict, IdempotentCommands
from app.bootstrap import create_job_platform_app
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


def _resources(tmp_path: Path):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    return create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"command-receipt-test-key-at-least-32-bytes",
    )


def _source_payload(name: str = "Primary") -> dict[str, object]:
    return {
        "name": name,
        "endpoints": [{"position": 0, "url": "https://am.invalid"}],
    }


def test_create_replays_first_response_and_rejects_key_reuse(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    headers = {"Idempotency-Key": "source-create-replay-0001"}
    with TestClient(resources.app) as client:
        first = client.post("/api/v1/sources", headers=headers, json=_source_payload())
        replay = client.post("/api/v1/sources", headers=headers, json=_source_payload())
        reused = client.post(
            "/api/v1/sources", headers=headers, json=_source_payload("Different")
        )
        missing = client.post("/api/v1/sources", json=_source_payload("Missing"))

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert missing.status_code == 422
    assert resources.sources.list_sources() == (
        resources.sources.get_source(first.json()["id"]),
    )
    database = (tmp_path / "incident-operations.db").read_bytes()
    assert b"source-create-replay-0001" not in database
    resources.engine.dispose()


def test_concurrent_duplicate_never_runs_action_twice(tmp_path: Path) -> None:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "incident-operations.db")
    )
    upgrade_database(engine)
    commands = IdempotentCommands(
        SqlAlchemyCommandReceiptStore(create_session_factory(engine))
    )
    entered = threading.Event()
    release = threading.Event()
    action_count = 0
    count_lock = threading.Lock()

    def action() -> dict[str, object]:
        nonlocal action_count
        with count_lock:
            action_count += 1
        entered.set()
        assert release.wait(timeout=2)
        return {"id": "one"}

    def execute() -> dict[str, object]:
        return dict(
            commands.execute(
                scope="source.create",
                key="source-create-concurrent-0001",
                payload={"name": "Primary"},
                action=action,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        assert entered.wait(timeout=2)
        second = pool.submit(execute)
        try:
            second.result(timeout=2)
        except CommandConflict as exc:
            assert exc.code == "COMMAND_OUTCOME_UNKNOWN"
        else:
            raise AssertionError("concurrent duplicate command was not rejected")
        release.set()
        assert first.result(timeout=2) == {"id": "one"}

    assert action_count == 1
    engine.dispose()


def test_external_failure_is_not_automatically_replayed(tmp_path: Path) -> None:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "incident-operations.db")
    )
    upgrade_database(engine)
    commands = IdempotentCommands(
        SqlAlchemyCommandReceiptStore(create_session_factory(engine))
    )
    calls = 0

    async def uncertain_provider_call() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise TimeoutError("provider response was not received")

    async def execute() -> None:
        await commands.execute_external(
            scope="notification-channel.test",
            key="notification-test-uncertain-0001",
            payload={"channel_id": "channel-a", "revision": 1},
            action=uncertain_provider_call,
        )

    try:
        asyncio.run(execute())
    except TimeoutError:
        pass
    else:
        raise AssertionError("provider timeout was not surfaced")

    try:
        asyncio.run(execute())
    except CommandConflict as exc:
        assert exc.code == "COMMAND_OUTCOME_UNKNOWN"
    else:
        raise AssertionError("ambiguous provider command was automatically replayed")

    assert calls == 1
    engine.dispose()


def test_db_create_crash_window_blocks_duplicate_instead_of_guessing_success(tmp_path: Path) -> None:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "incident-operations.db")
    )
    upgrade_database(engine)
    receipts = SqlAlchemyCommandReceiptStore(create_session_factory(engine))
    commands = IdempotentCommands(receipts)
    receipt = receipts.claim(
        "source.create", "source-create-crash-window", '{"name":"Primary"}'
    )
    assert receipt.replay is None
    with pytest.raises(CommandConflict, match="COMMAND_OUTCOME_UNKNOWN"):
        commands.execute(
            scope="source.create",
            key="source-create-crash-window",
            payload={"name": "Primary"},
            action=lambda: {"id": "must-not-run"},
        )
    engine.dispose()
