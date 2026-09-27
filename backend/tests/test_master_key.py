"""The master key is generated state, not configuration.

A user should never have to know it exists; an upgrade must never lose the
one that already encrypted their stored credentials.
"""

from __future__ import annotations

import stat

import pytest

from app import master_key as master_key_module
from app.config import settings


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "master_key", "")
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "workbench.db"))
    monkeypatch.setattr(master_key_module, "_LEGACY_ENV_FILE", tmp_path / "absent.env")
    master_key_module.reset_cache()
    yield tmp_path
    master_key_module.reset_cache()


def test_first_use_generates_a_key_file_the_user_never_has_to_touch(key_dir) -> None:
    generated = master_key_module.master_key()

    path = key_dir / "master.key"
    assert len(generated) >= 32
    assert path.read_text(encoding="utf-8").strip() == generated
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_existing_key_is_never_regenerated(key_dir) -> None:
    """Rotating it would make every stored credential unreadable."""
    first = master_key_module.master_key()
    master_key_module.reset_cache()

    assert master_key_module.master_key() == first
    assert (key_dir / "master.key").read_text(encoding="utf-8").strip() == first


def test_an_environment_override_never_touches_the_disk(key_dir, monkeypatch) -> None:
    monkeypatch.setattr(settings, "master_key", "explicit-process-key")

    assert master_key_module.master_key() == "explicit-process-key"
    assert not (key_dir / "master.key").exists()


def test_an_old_env_file_hands_its_key_over_once(key_dir, monkeypatch) -> None:
    legacy = key_dir / "legacy.env"
    legacy.write_text(
        "ALERT_WORKBENCH_DATABASE_PATH=data/workbench.db\n"
        "ALERT_WORKBENCH_MASTER_KEY=key-that-decrypts-existing-secrets\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(master_key_module, "_LEGACY_ENV_FILE", legacy)

    resolved = master_key_module.master_key()

    assert resolved == "key-that-decrypts-existing-secrets"
    assert (key_dir / "master.key").read_text(encoding="utf-8").strip() == resolved
    assert master_key_module.carried_over_from_env is True


def test_the_key_file_wins_over_a_stale_env_file(key_dir, monkeypatch) -> None:
    (key_dir / "master.key").write_text("already-migrated-key\n", encoding="utf-8")
    legacy = key_dir / "legacy.env"
    legacy.write_text("ALERT_WORKBENCH_MASTER_KEY=stale\n", encoding="utf-8")
    monkeypatch.setattr(master_key_module, "_LEGACY_ENV_FILE", legacy)

    assert master_key_module.master_key() == "already-migrated-key"


def test_an_unwritable_location_fails_closed(key_dir, monkeypatch) -> None:
    """No key means no readable credentials -- say so instead of improvising."""
    from app.crypto import SecretUnavailableError

    monkeypatch.setattr(
        settings, "database_path", str(key_dir / "missing" / "workbench.db")
    )
    monkeypatch.setattr(
        master_key_module.Path, "mkdir", lambda *args, **kwargs: None
    )

    with pytest.raises(SecretUnavailableError):
        master_key_module.master_key()
