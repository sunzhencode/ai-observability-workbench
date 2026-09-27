"""Shared pytest fixtures: offline sample alerts and an in-memory database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import master_key
from app.config import settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def stable_offline_settings(monkeypatch, tmp_path):
    """Keep offline tests off the developer's real database and key file.

    ``database_path`` matters beyond the database now: the master key file
    lives beside it, so an unpointed test run would create -- or worse, read --
    the real one.
    """
    monkeypatch.setattr(settings, "alertmanager_url", "http://alertmanager.test")
    monkeypatch.setattr(settings, "thanos_url", "")
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "workbench.db"))
    monkeypatch.setattr(settings, "master_key", "offline-test-master-key")
    master_key.reset_cache()
    yield
    master_key.reset_cache()


@pytest.fixture
def sample_alerts() -> list[dict[str, Any]]:
    with open(FIXTURES / "alertmanager_sample.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def session():
    # Import models so tables register before create_all.
    from app import models  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
