"""F20 source-bound Incident group-key construction."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote


def build_group_key_v2(
    *,
    source_id: str,
    rule_id: int | None,
    ordered_group_values: Iterable[tuple[str, str]] = (),
    isolation_fingerprint: str | None = None,
) -> str:
    """Build the environment-free, source-bound F20 group key."""

    parts = [f"source={quote(str(source_id or 'legacy'), safe='')}"]
    parts.append("unmatched" if rule_id is None else f"rule={int(rule_id)}")
    for name, value in ordered_group_values:
        parts.append(f"{quote(str(name), safe='')}={quote(str(value), safe='')}")
    if isolation_fingerprint is not None:
        parts.append(
            f"isolation=fingerprint:{quote(str(isolation_fingerprint), safe='')}"
        )
    return "|".join(parts)
