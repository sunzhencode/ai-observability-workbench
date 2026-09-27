"""Dry-run-first archive/reset/recovery CLI for an explicit Incident Operations cutover."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.platform.persistence.archive import (
    ArchiveSafetyError,
    plan_database_reset,
    reset_database,
    restore_database,
)
from app.platform.persistence.inventory import build_configuration_inventory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incident-operations-persistence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory", help="print a read-only secret-free configuration inventory"
    )
    inventory.add_argument("--database", type=Path, required=True)

    reset = subparsers.add_parser("reset", help="archive the old DB and create Incident Operations")
    reset.add_argument("--database", type=Path, required=True)
    reset.add_argument("--archive-root", type=Path)
    reset.add_argument("--master-key", type=Path)
    reset.add_argument("--execute", action="store_true")
    reset.add_argument("--confirm-process-stopped", action="store_true")

    restore = subparsers.add_parser("restore", help="restore an archived DB set")
    restore.add_argument("--archive-directory", type=Path, required=True)
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--master-key", type=Path, required=True)
    restore.add_argument("--execute", action="store_true")
    restore.add_argument("--confirm-process-stopped", action="store_true")
    return parser


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            _print(build_configuration_inventory(args.database))
            return 0

        if args.command == "reset":
            plan = plan_database_reset(
                database_path=args.database,
                archive_root=args.archive_root,
                master_key_path=args.master_key,
            )
            if not args.execute:
                _print(plan.as_dict())
                return 0
            if not args.confirm_process_stopped:
                parser.error("--execute requires --confirm-process-stopped")
            result = reset_database(plan, confirm_process_stopped=True)
            _print(
                {
                    "executed": True,
                    "migration_revision": result.migration_revision,
                    "archive": result.archive_manifest.as_dict(),
                }
            )
            return 0

        if not args.execute:
            _print(
                {
                    "action": "restore_archived_database",
                    "archive_directory": str(args.archive_directory.resolve()),
                    "database_path": str(args.database.resolve()),
                    "master_key": {"path": str(args.master_key.resolve()), "moved": False},
                    "executed": False,
                }
            )
            return 0
        if not args.confirm_process_stopped:
            parser.error("--execute requires --confirm-process-stopped")
        restored = restore_database(
            archive_directory=args.archive_directory,
            database_path=args.database,
            master_key_path=args.master_key,
            confirm_process_stopped=True,
        )
        _print({"executed": True, "restored": [str(path) for path in restored]})
        return 0
    except ArchiveSafetyError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
