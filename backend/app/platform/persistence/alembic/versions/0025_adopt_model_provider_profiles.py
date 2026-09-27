"""Adopt pre-harness model channel revisions into explicit provider profiles.

Revision ID: platform_0025
Revises: platform_0024
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision: str = "platform_0025"
down_revision: str | None = "platform_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KNOWN_PROVIDERS: dict[str, tuple[str, str, str]] = {
    "https://api.openai.com/v1": ("OPENAI", "RESPONSES", "REVIEWED"),
    "https://api.deepseek.com": ("DEEPSEEK", "CHAT_COMPLETIONS", "REVIEWED"),
    "https://api.moonshot.cn/v1": ("MOONSHOT", "CHAT_COMPLETIONS", "REVIEWED"),
    "https://open.bigmodel.cn/api/paas/v4": ("ZHIPU", "CHAT_COMPLETIONS", "REVIEWED"),
    "https://dashscope.aliyuncs.com/compatible-mode/v1": (
        "DASHSCOPE",
        "CHAT_COMPLETIONS",
        "COMPATIBLE",
    ),
}


def _profile_values(base_url: str, model: str) -> tuple[str, str, str, str, str]:
    canonical_url = base_url.strip().rstrip("/")
    provider_id, protocol, support = _KNOWN_PROVIDERS.get(
        canonical_url,
        ("CUSTOM", "CHAT_COMPLETIONS", "BEST_EFFORT"),
    )
    settings: dict[str, object] = {"parallel_tool_calls": False}
    if provider_id == "OPENAI":
        settings.update({"openai_store": False, "openai_truncation": "disabled"})
    if provider_id == "MOONSHOT" and model.lower() in {"kimi-k2.5", "kimi-k2.6"}:
        settings["extra_body"] = {"thinking": {"type": "disabled"}}
    settings_json = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return provider_id, protocol, support, canonical_url, settings_json


def _profile_id(signature: str) -> str:
    return f"provider-adopted-{hashlib.sha256(signature.encode()).hexdigest()[:32]}"


def upgrade() -> None:
    connection = op.get_bind()
    revisions = connection.execute(
        sa.text(
            """
            SELECT id, base_url, model, created_at
            FROM model_channel_revision
            WHERE provider_profile_id IS NULL
            ORDER BY id
            """
        )
    ).mappings()
    next_revision: dict[str, int] = {
        str(row.provider_id): int(row.latest_revision or 0) + 1
        for row in connection.execute(
            sa.text(
                """
                SELECT provider_id, MAX(revision) AS latest_revision
                FROM provider_profile
                GROUP BY provider_id
                """
            )
        ).mappings()
    }
    profiles: dict[tuple[str, str, str, str, str], str] = {
        (
            str(row.provider_id),
            str(row.protocol_profile),
            str(row.support_level),
            str(row.base_url),
            str(row.settings_json),
        ): str(row.id)
        for row in connection.execute(
            sa.text(
                """
                SELECT id, provider_id, protocol_profile, support_level,
                       base_url, settings_json
                FROM provider_profile
                """
            )
        ).mappings()
    }

    for channel_revision in revisions:
        values = _profile_values(
            str(channel_revision.base_url),
            str(channel_revision.model),
        )
        profile_id = profiles.get(values)
        if profile_id is None:
            provider_id, protocol, support, base_url, settings_json = values
            provider_revision = next_revision.get(provider_id, 1)
            signature = "\n".join(values)
            profile_id = _profile_id(signature)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO provider_profile (
                      id, provider_id, protocol_profile, support_level, base_url,
                      settings_json, revision, created_at
                    ) VALUES (
                      :id, :provider_id, :protocol_profile, :support_level,
                      :base_url, :settings_json, :revision, :created_at
                    )
                    """
                ),
                {
                    "id": profile_id,
                    "provider_id": provider_id,
                    "protocol_profile": protocol,
                    "support_level": support,
                    "base_url": base_url,
                    "settings_json": settings_json,
                    "revision": provider_revision,
                    "created_at": channel_revision.created_at,
                },
            )
            profiles[values] = profile_id
            next_revision[provider_id] = provider_revision + 1
        connection.execute(
            sa.text(
                """
                UPDATE model_channel_revision
                SET provider_profile_id = :profile_id
                WHERE id = :revision_id AND provider_profile_id IS NULL
                """
            ),
            {"profile_id": profile_id, "revision_id": channel_revision.id},
        )


def downgrade() -> None:
    # This is an additive adoption of existing product facts. Keeping the links is
    # safe at platform_0024 and avoids invalidating investigation audit history.
    pass
