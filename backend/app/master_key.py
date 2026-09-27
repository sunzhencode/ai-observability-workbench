"""The one secret that cannot live in the database it protects.

Everything else the workbench needs to know is configured in the UI and stored
in SQLite.  The key that encrypts those stored credentials obviously cannot be,
so it is the last piece of local state outside the database -- but it is state,
not configuration: nobody has to choose it, write it down or put it in a file.

Resolution order, fixed:

1. ``ALERT_WORKBENCH_MASTER_KEY`` in the environment (tests and ``--mock``);
2. ``<database dir>/master.key``;
3. one-time carry-over of the key from a pre-F22 ``backend/.env``;
4. a fresh random key written with ``O_EXCL`` and mode ``0600``.

An existing key file is never overwritten or rotated: doing so would make every
stored credential permanently unreadable.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from app.config import settings
from app.crypto import SecretUnavailableError

MASTER_KEY_FILENAME = "master.key"
_ENV_KEY = "ALERT_WORKBENCH_MASTER_KEY"
_LEGACY_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

_cache: dict[str, str] = {}
carried_over_from_env = False


def master_key_path() -> Path:
    """The key lives beside the database it protects."""
    database = Path(settings.database_path)
    directory = database.parent if str(database.parent) else Path(".")
    return directory / MASTER_KEY_FILENAME


def _legacy_env_key() -> str:
    """Read only ``ALERT_WORKBENCH_MASTER_KEY`` out of a pre-F22 ``.env``."""
    try:
        lines = _LEGACY_ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(_ENV_KEY):
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == _ENV_KEY:
            return value.strip().strip("'\"")
    return ""


def _write_new_key(path: Path, value: str) -> str:
    """Create the key file, or read the winner if someone else got there first."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SecretUnavailableError(
            "master key file could not be created"
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def _resolve() -> str:
    global carried_over_from_env

    path = master_key_path()
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    carried = _legacy_env_key()
    if carried:
        written = _write_new_key(path, carried)
        carried_over_from_env = written == carried
        return written
    return _write_new_key(path, secrets.token_urlsafe(48))


def master_key() -> str:
    """The local master key, generating one on first use."""
    override = str(settings.master_key or "").strip()
    if override:
        return override
    key = str(master_key_path())
    if key not in _cache:
        _cache[key] = _resolve()
    return _cache[key]


def reset_cache() -> None:
    """Test seam: forget the resolved key so a new path can be resolved."""
    global carried_over_from_env

    _cache.clear()
    carried_over_from_env = False
