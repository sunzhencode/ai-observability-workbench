"""Bootstrap values: where to put the database and which ports to listen on.

The split is the point.  Everything a *user* configures -- data sources,
addresses, credentials, rules, notification routing -- lives in the local
SQLite registry. Data sources are edited in 系统设置; rules and notifications
have their own pages (ADR 0004, ADR 0005).  This file
holds only what has to be known *before* the database can be opened, plus the
switches ``start.sh --mock`` and the tests set in the environment.

``backend/.env`` is optional: every field below has a working default, so a
fresh clone runs without one.  Create it only to move the database or the
ports.  See ``backend/.env.example``.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bootstrap settings from ``ALERT_WORKBENCH_`` env vars or an optional .env."""

    model_config = SettingsConfigDict(
        env_prefix="ALERT_WORKBENCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: str = "data/workbench.db"
    host: str = "127.0.0.1"
    port: int = 8000
    # The Vite dev server's port. It lives here so `.env` has one parser and
    # start.sh has one place to ask; the backend itself never uses it.
    frontend_port: int = 5173

    # Empty means "use the local key file" -- see app/master_key.py. An
    # explicit value overrides it without ever touching disk, which is how the
    # tests and `--mock` avoid the real one.
    master_key: str = ""

    # Developer switches. Default local-safe mode and `--mock` set providers to
    # FAKE so no real notification request can leave the machine.
    notification_provider_mode: str = "FEISHU"
    notification_fake_channel_test_script: str = "OK"
    notification_fake_delivery_script: str = "OK"
    # F27 model outbound. Mirrors notification: local-safe and `--mock` force
    # FAKE so local acceptance cannot reach a real model service.
    model_provider_mode: str = "REAL"
    model_fake_script: str = "OK"

    # --- read only by migration v6 ------------------------------------------
    # An applied migration's checksum covers its own source, so the adoption
    # function that reads these fields can never be edited -- and a fresh
    # database replays it too, which means deleting the fields would break
    # startup with an AttributeError. Runtime code must not read them: with no
    # .env and nobody setting these variables they stay empty, so adoption
    # produces no source on a new database. That blank start is the point.
    alertmanager_url: str = ""
    alertmanager_token: str = ""
    thanos_url: str = ""
    thanos_token: str = ""
    thanos_timeout_seconds: float = 15.0
    poll_interval_seconds: int = 30
    resolution_grace_seconds: int = 300


settings = Settings()
