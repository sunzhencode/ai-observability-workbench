"""Migration v9 and the per-provider config models.

The property that matters for a real database: a channel configured before F24
must keep working without the user re-entering a webhook they cannot read back.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine, select

from app.db import create_sqlite_engine
from app.migrations import MIGRATIONS, run_migrations
from app.models import NotificationChannel, NotificationChannelRevision
from app.providers.configs import (
    FeishuChannelConfig,
    GenericWebhookChannelConfig,
    SmtpChannelConfig,
    parse_channel_config,
)


def _migrated(tmp_path, name="workbench.db"):
    """A fully migrated database, the way the app builds one at startup."""

    path = tmp_path / name
    engine = create_sqlite_engine(path)
    return engine, path, run_migrations(engine, database_path=path)


def _pre_v9(tmp_path, name="pre-f24.db"):
    """A database migrated to v8 only -- the state v9 has to upgrade."""

    import app.migrations as migration_module

    original = migration_module.MIGRATIONS
    migration_module.MIGRATIONS = tuple(
        item for item in original if item.version < 9
    )
    try:
        path = tmp_path / name
        engine = create_sqlite_engine(path)
        run_migrations(engine, database_path=path)
    finally:
        migration_module.MIGRATIONS = original
    return engine, path


class TestMigrationV9:
    def test_it_is_appended_after_everything_that_predates_it(self) -> None:
        """v9 exists, is unique and sits after v8 -- not that it is the head.

        This used to assert `versions[-1] == 9`, which made every future
        migration break an F24 test for no reason (F26's v10 did exactly that).
        What the ledger actually guarantees is append-only ordering, so that is
        what gets pinned here.
        """
        versions = [item.version for item in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)
        assert 9 in versions
        assert versions.index(9) == versions.index(8) + 1

    def test_existing_feishu_revisions_keep_their_credential(self, tmp_path) -> None:
        engine, path = _pre_v9(tmp_path)
        with Session(engine) as session:
            channel = NotificationChannel(name="ops")
            session.add(channel)
            session.flush()
            revision = NotificationChannelRevision(
                channel_id=channel.id,
                version=1,
                state="ACTIVE",
                webhook_envelope={"version": 1, "ciphertext": "abc"},
                signing_secret_envelope={"version": 1, "ciphertext": "sig"},
                required_keyword="alert",
                mention_mode="USERS",
                mention_users=[{"open_id": "ou_1"}],
                mention_on={"FIRING_OPENED": True},
                config_envelope={},
            )
            session.add(revision)
            session.commit()
            revision_id = revision.id

        report = run_migrations(engine, database_path=path)
        assert 9 in report.applied_versions

        with Session(engine) as session:
            migrated = session.get(NotificationChannelRevision, revision_id)
            config = parse_channel_config(migrated.provider, migrated.config_envelope)
            assert isinstance(config, FeishuChannelConfig)
            # The credential is carried over verbatim -- the user is never asked
            # to retype a webhook they cannot read back.
            assert config.webhook == {"version": 1, "ciphertext": "abc"}
            assert config.signing_secret == {"version": 1, "ciphertext": "sig"}
            assert config.required_keyword == "alert"
            assert config.mention_mode == "USERS"
            assert config.mention_users == [{"open_id": "ou_1"}]
            assert config.mention_on == {"FIRING_OPENED": True}
            # The legacy columns stay untouched: they are the history of what an
            # old revision held.
            assert migrated.webhook_envelope == {"version": 1, "ciphertext": "abc"}

    def test_it_does_not_overwrite_an_envelope_that_is_already_there(
        self, tmp_path
    ) -> None:
        engine, path = _pre_v9(tmp_path)
        with Session(engine) as session:
            channel = NotificationChannel(name="ops")
            session.add(channel)
            session.flush()
            session.add(
                NotificationChannelRevision(
                    channel_id=channel.id,
                    version=1,
                    state="ACTIVE",
                    webhook_envelope={"version": 1, "ciphertext": "stale"},
                    config_envelope={
                        "kind": "FEISHU_CUSTOM_BOT",
                        "webhook": {"version": 1, "ciphertext": "current"},
                    },
                )
            )
            session.commit()

        run_migrations(engine, database_path=path)

        with Session(engine) as session:
            revision = session.exec(select(NotificationChannelRevision)).one()
            assert revision.config_envelope["webhook"]["ciphertext"] == "current"

    def test_repeated_startups_are_stable(self, tmp_path) -> None:
        engine, path, first = _migrated(tmp_path)
        # v9 is among what a pre-v9 database applies; it need not be the last one.
        assert 9 in first.applied_versions
        for _ in range(3):
            again = run_migrations(engine, database_path=path)
            assert again.applied_versions == []


class TestConfigModels:
    def test_smtp_ports_are_pinned_to_their_tls_mode(self) -> None:
        base = {
            "host": "smtp.example.com",
            "from_addr": "bot@example.com",
            "to_addrs": ["ops@example.com"],
        }
        assert SmtpChannelConfig(port=587, tls_mode="STARTTLS", **base).port == 587
        assert SmtpChannelConfig(port=465, tls_mode="TLS", **base).port == 465
        with pytest.raises(ValueError):
            SmtpChannelConfig(port=25, tls_mode="STARTTLS", **base)
        with pytest.raises(ValueError):
            SmtpChannelConfig(port=465, tls_mode="STARTTLS", **base)

    def test_smtp_rejects_something_that_is_not_an_address(self) -> None:
        with pytest.raises(ValueError):
            SmtpChannelConfig(
                host="smtp.example.com",
                port=587,
                from_addr="not-an-address",
                to_addrs=["ops@example.com"],
            )

    def test_generic_webhook_runs_the_egress_guard_at_save_time(self) -> None:
        for url in [
            "http://example.com/hook",  # not https
            "https://example.com:8443/hook",  # port
            "https://user:pw@example.com/hook",  # credentials in the url
        ]:
            with pytest.raises(ValueError):
                GenericWebhookChannelConfig(url=url)

    def test_headers_are_treated_as_a_secret(self) -> None:
        config = GenericWebhookChannelConfig(
            url="https://example.com/hook",
            headers={"version": 1, "ciphertext": "token"},
        )
        # A token in a header is the commonest webhook auth; it must be in the
        # set of things re-encryption and redaction know about.
        assert config.secret_envelopes() == [{"version": 1, "ciphertext": "token"}]

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError):
            parse_channel_config("SLACK", {})

    def test_a_blank_kind_reads_as_feishu(self) -> None:
        assert isinstance(parse_channel_config("", {}), FeishuChannelConfig)

    def test_extra_fields_are_refused(self) -> None:
        with pytest.raises(ValueError):
            FeishuChannelConfig(webhook={}, surprise="x")

    def test_a_config_serialises_without_opening_its_secrets(self) -> None:
        config = FeishuChannelConfig(webhook={"version": 1, "ciphertext": "abc"})
        dumped = json.dumps(config.model_dump())
        # Envelopes are ciphertext; nothing here decrypts. The send path is the
        # only place that opens them.
        assert "ciphertext" in dumped
        assert config.secret_envelopes() == [{"version": 1, "ciphertext": "abc"}]
