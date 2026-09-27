"""Prompt Profile safety kernel, lifecycle and local preview contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.investigations.prompt_profiles import (
    IMMUTABLE_LAYERS,
    PromptGuidanceV1,
    builtin_profile,
    preview_profile,
    profile_contract_code,
)
from app.bootstrap import create_job_platform_app


def test_builtin_profile_is_active_read_only_and_has_no_freeform_kernel() -> None:
    profile = builtin_profile()
    rendered = preview_profile(profile)

    assert profile.builtin is True
    assert profile.status == "ACTIVE"
    assert rendered.safety_kernel == IMMUTABLE_LAYERS
    assert rendered.operator_guidance == {}
    assert rendered.maximum_cost == "UNKNOWN"


def test_custom_guidance_is_limited_to_four_product_fields() -> None:
    guidance = PromptGuidanceV1(
        organization_context="结算服务由支付平台团队维护",
        investigation_focus="先核对错误率，再核对流量",
        terminology="Occurrence 称为本次事件",
        response_style="简洁列出反证",
    )
    profile = replace(
        builtin_profile(),
        id="profile-checkout",
        name="结算服务",
        builtin=False,
        status="DRAFT",
        guidance=guidance,
    )

    rendered = preview_profile(profile)

    assert set(rendered.operator_guidance) == {
        "organization_context",
        "investigation_focus",
        "terminology",
        "response_style",
    }
    assert not set(rendered.operator_guidance) & set(IMMUTABLE_LAYERS)
    assert profile_contract_code(profile) == "OK"


def test_profile_field_limit_is_enforced_by_code() -> None:
    with pytest.raises(ValueError, match="PROMPT_PROFILE_FIELD_TOO_LONG"):
        PromptGuidanceV1(organization_context="x" * 2_001)


def test_profile_copy_preview_optional_test_and_activation_are_simple(
    tmp_path: Path,
) -> None:
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "operations.db",
        master_key_path=key,
        cursor_secret=b"prompt-profile-test-cursor-secret-32",
    )

    with TestClient(resources.app) as client:
        builtin = client.get("/api/v1/prompt-profiles").json()
        assert builtin == [
            {
                "id": "builtin-standard",
                "name": "内置标准",
                "builtin": True,
                "status": "ACTIVE",
                "revision": 1,
                "active_revision": 1,
                "guidance": {
                    "organization_context": "",
                    "investigation_focus": "",
                    "terminology": "",
                    "response_style": "",
                },
                "is_global_default": True,
                "service_ids": [],
                "tested_at": None,
                "last_test_code": "BUILTIN_CONTRACT_OK",
            }
        ]
        copied_response = client.post(
            "/api/v1/prompt-profiles/copy-standard", json={"name": "结算服务调查"}
        )
        assert copied_response.status_code == 201, copied_response.text
        copied = copied_response.json()
        assert copied["status"] == "DRAFT"

        updated_response = client.put(
            f"/api/v1/prompt-profiles/{copied['id']}/draft",
            json={
                "expected_revision": copied["revision"],
                "organization_context": "结算服务由支付平台团队维护",
                "investigation_focus": "先核对错误率，再核对流量",
                "terminology": "Occurrence 称为本次事件",
                "response_style": "简洁列出反证",
            },
        )
        assert updated_response.status_code == 200, updated_response.text
        updated = updated_response.json()
        assert updated["revision"] == 2

        preview = client.post(
            f"/api/v1/prompt-profiles/{copied['id']}/preview",
            json={"expected_revision": 2},
        ).json()
        assert preview["maximum_cost"] == "UNKNOWN"
        assert "模型无写边界" in preview["safety_kernel"]
        assert preview["operator_guidance"]["investigation_focus"].startswith("先核对")

        # Test is local diagnostics only: it records a result and leaves DRAFT.
        tested = client.post(
            f"/api/v1/prompt-profiles/{copied['id']}/test",
            json={"expected_revision": 2},
        ).json()
        assert tested["last_test_code"] == "OK"
        assert tested["status"] == "DRAFT"

        activated = client.post(
            f"/api/v1/prompt-profiles/{copied['id']}/activate",
            json={"expected_revision": 2, "global_default": True, "service_ids": []},
        ).json()
        assert activated["status"] == "ACTIVE"
        assert activated["active_revision"] == 2
        assert activated["is_global_default"] is True

        next_draft = client.put(
            f"/api/v1/prompt-profiles/{copied['id']}/draft",
            json={
                "expected_revision": 2,
                "organization_context": "更新中的背景",
                "investigation_focus": "",
                "terminology": "",
                "response_style": "",
            },
        ).json()
        assert next_draft["status"] == "DRAFT"
        assert next_draft["active_revision"] == 2
        listed = client.get("/api/v1/prompt-profiles").json()
        listed_custom = next(item for item in listed if item["id"] == copied["id"])
        assert listed_custom["active_revision"] == 2
        assert listed[0]["is_global_default"] is False
        revisions_response = client.get(
            f"/api/v1/prompt-profiles/{copied['id']}/revisions"
        )
        assert revisions_response.status_code == 200, revisions_response.text
        revisions = revisions_response.json()
        assert [item["revision"] for item in revisions] == [3, 2, 1]
        assert revisions[0]["status"] == "DRAFT"
        assert revisions[0]["changed_fields"] == [
            "organization_context",
            "investigation_focus",
            "terminology",
            "response_style",
        ]
        assert revisions[1]["status"] == "ACTIVE"
        assert revisions[1]["changed_fields"] == [
            "organization_context",
            "investigation_focus",
            "terminology",
            "response_style",
        ]
        assert revisions[2]["changed_fields"] == []


def test_activation_does_not_require_connection_or_contract_test(tmp_path: Path) -> None:
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "operations.db",
        master_key_path=key,
        cursor_secret=b"prompt-profile-test-cursor-secret-32",
    )
    with TestClient(resources.app) as client:
        copied = client.post(
            "/api/v1/prompt-profiles/copy-standard", json={"name": "可直接启用"}
        ).json()
        activated = client.post(
            f"/api/v1/prompt-profiles/{copied['id']}/activate",
            json={"expected_revision": 1, "global_default": False, "service_ids": []},
        )
    assert activated.status_code == 200, activated.text
    assert activated.json()["last_test_code"] is None
