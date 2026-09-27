"""What may live in a file, and what may not.

``backend/.env`` carries bootstrap values only -- where the database is and
which ports to bind. Data source addresses and credentials belong to the
registry (ADR 0004); the seven fields that survive on ``Settings`` exist only
because migration v6 reads them and an applied migration's source cannot
change. If runtime code starts reading those again, an address has quietly
grown a second home outside the registry, which is the exact defect ADR 0004
was written about.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

BACKEND = Path(__file__).resolve().parents[1]
EXAMPLE = BACKEND / ".env.example"

BOOTSTRAP_KEYS = {
    "INCIDENT_OPERATIONS_DATABASE_PATH",
    "INCIDENT_OPERATIONS_HOST",
    "INCIDENT_OPERATIONS_PORT",
    "INCIDENT_OPERATIONS_FRONTEND_PORT",
}

MIGRATION_ONLY_FIELDS = (
    "alertmanager_url",
    "alertmanager_token",
    "thanos_url",
    "thanos_token",
    "thanos_timeout_seconds",
    "poll_interval_seconds",
    "resolution_grace_seconds",
)

# Reading these is legitimate here only: the migration itself, the class that
# declares them, and the runtime constants that replaced them by name.
ALLOWED = {"migrations.py", "config.py", "runtime_config.py"}


def _runtime_sources() -> list[Path]:
    return [
        path
        for path in (BACKEND / "app").rglob("*.py")
        if path.name not in ALLOWED
    ]


def _example_keys() -> set[str]:
    """Every uncommented INCIDENT_OPERATIONS_* key in the template."""
    return set(
        re.findall(
            r"^(INCIDENT_OPERATIONS_[A-Z_]+)=",
            EXAMPLE.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )


def test_the_template_offers_bootstrap_values_and_nothing_else() -> None:
    assert EXAMPLE.exists()
    assert _example_keys() == BOOTSTRAP_KEYS


def test_the_template_never_invites_a_data_source_back_into_a_file() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")

    for banned in ("ALERTMANAGER_URL", "ALERTMANAGER_TOKEN", "THANOS_URL", "GRAFANA"):
        assert banned not in text, f"{banned} belongs in the registry, not a file"
    # The master key may be mentioned, but only as a commented-out override.
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "MASTER_KEY" not in line


def test_runtime_code_never_reads_the_migration_only_fields() -> None:
    offenders: list[str] = []
    for path in _runtime_sources():
        text = path.read_text(encoding="utf-8")
        for field in MIGRATION_ONLY_FIELDS:
            if f"settings.{field}" in text:
                offenders.append(f"{path.relative_to(BACKEND)}: settings.{field}")
    assert offenders == []


def test_a_missing_env_file_still_starts() -> None:
    """The file is optional: a fresh clone runs on the defaults."""
    defaults = Settings(_env_file=None)

    assert defaults.database_path == "data/workbench.db"
    assert defaults.host == "127.0.0.1"
    assert defaults.port == 8000
    assert defaults.frontend_port == 5173
    # Empty means "use the local key file", never "read it from somewhere".
    assert defaults.master_key == ""
    # Adoption finds nothing, so a fresh database starts blank on purpose.
    assert defaults.alertmanager_url == ""


def test_an_env_file_overrides_the_defaults(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALERT_WORKBENCH_PORT=18000\nALERT_WORKBENCH_FRONTEND_PORT=15173\n",
        encoding="utf-8",
    )

    configured = Settings(_env_file=env_file)

    assert configured.port == 18000
    assert configured.frontend_port == 15173
