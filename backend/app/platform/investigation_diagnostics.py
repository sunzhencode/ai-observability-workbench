"""Stopped-process local diagnostic access to an encrypted rejected Analyst reply."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import UUID

from app.crypto import SecretBox

UTC = timezone.utc


def read_invalid_response(
    *, database_path: Path, master_key_path: Path, investigation_id: str
) -> str:
    try:
        exact_id = str(UUID(investigation_id))
    except ValueError:
        raise ValueError("INVESTIGATION_ID_INVALID") from None
    if exact_id != investigation_id:
        raise ValueError("INVESTIGATION_ID_INVALID")
    database = Path(database_path).expanduser().resolve()
    key = Path(master_key_path).expanduser().resolve()
    if not database.is_file() or not key.is_file():
        raise FileNotFoundError("DIAGNOSTIC_INPUT_MISSING")
    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT envelope_json,purge_after FROM invalid_analyst_response "
            "WHERE investigation_id=?",
            (exact_id,),
        ).fetchone()
    if row is None:
        raise LookupError("INVALID_ANALYST_RESPONSE_NOT_FOUND")
    purge_after = datetime.fromisoformat(str(row[1])).replace(tzinfo=UTC)
    if purge_after <= datetime.now(UTC):
        raise LookupError("INVALID_ANALYST_RESPONSE_EXPIRED")
    envelope: Any = json.loads(str(row[0]))
    if not isinstance(envelope, dict):
        raise RuntimeError("INVALID_ANALYST_RESPONSE_ENVELOPE_INVALID")
    box = SecretBox(key.read_text(encoding="utf-8").strip())
    return box.decrypt(envelope)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read one rejected Analyst reply locally. Stop the Operations Console first; "
            "the reply may contain sensitive monitoring data."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--master-key", type=Path, required=True)
    parser.add_argument("--investigation-id", required=True)
    parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Confirm the Operations Console using this database has been stopped.",
    )
    args = parser.parse_args()
    if not args.confirm_stopped:
        parser.error("--confirm-stopped is required")
    print(
        read_invalid_response(
            database_path=args.database,
            master_key_path=args.master_key,
            investigation_id=args.investigation_id,
        )
    )


if __name__ == "__main__":
    main()
