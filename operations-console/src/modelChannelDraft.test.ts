import { describe, expect, it } from "vitest";
import {
  availableModelOptions,
  canSaveModelChannel,
  emptyModelChannelForm,
  filterDiscoveredModels,
  isInternalFakeModel,
  latestModelRevision,
  modelChannelDraft,
  modelChannelFormFrom,
  modelOptionGroups,
} from "./modelChannelDraft";
import type { ModelChannel } from "./types";

function channel(): ModelChannel {
  return {
    id: "model-primary",
    name: "primary",
    kind: "OPENAI_COMPATIBLE",
    enabled: false,
    active_revision_id: null,
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:00:00Z",
    revisions: [
      {
        id: "model-primary",
        version: 1,
        state: "DRAFT",
        base_url: "https://model.example.com/v1",
        base_host: "model.example.com",
        model: "model-a",
        secret_configured: true,
        tested_ok_at: null,
        last_tested_at: null,
        last_test_code: null,
        created_at: "2026-07-31T00:00:00Z",
        provider_profile_id: "provider-custom-1",
        provider_id: "CUSTOM",
        protocol_profile: "CHAT_COMPLETIONS",
        support_level: "BEST_EFFORT",
      },
    ],
  };
}

describe("model channel draft", () => {
  it("requires a write-only key for a new channel", () => {
    const form = {
      ...emptyModelChannelForm(),
      name: "primary",
      baseUrl: "https://model.example.com/v1",
      model: "model-a",
    };
    expect(canSaveModelChannel(form)).toBe(false);
    form.apiKey = { configured: false, input: "test-value", cleared: false };
    expect(canSaveModelChannel(form)).toBe(true);
    expect(modelChannelDraft(form).config.api_key.action).toBe("REPLACE");
  });

  it("never reconstructs a stored key and leaves an empty box as KEEP", () => {
    const form = modelChannelFormFrom(channel());
    expect(form.apiKey.input).toBe("");
    expect(form.apiKey.configured).toBe(true);
    expect(modelChannelDraft(form).config.api_key).toEqual({ action: "KEEP" });
  });

  it("uses the newest revision returned by the API", () => {
    expect(latestModelRevision(channel())?.id).toBe("model-primary");
  });
});

describe("save-and-enable requirements", () => {
  it("requires a model name because saving immediately enables the service", () => {
    const form = {
      name: "openai",
      providerId: "OPENAI" as const,
      baseUrl: "https://api.openai.com/v1",
      model: "",
      apiKey: { input: "k-secret", configured: false, cleared: false },
    };
    expect(canSaveModelChannel(form)).toBe(false);
  });

  it("still needs a name, an address and a key", () => {
    const base = {
      name: "openai",
      providerId: "OPENAI" as const,
      baseUrl: "https://api.openai.com/v1",
      model: "gpt-4o",
      apiKey: { input: "k", configured: false, cleared: false },
    };
    expect(canSaveModelChannel({ ...base, name: "  " })).toBe(false);
    expect(canSaveModelChannel({ ...base, baseUrl: "" })).toBe(false);
    expect(
      canSaveModelChannel({
        ...base,
        apiKey: { input: "", configured: false, cleared: false },
      }),
    ).toBe(false);
  });

  it("rejects local fake implementation names as a saved provider model", () => {
    const form = {
      name: "openai",
      providerId: "OPENAI" as const,
      baseUrl: "https://api.openai.com/v1",
      model: "fake-planner",
      apiKey: { input: "k-secret", configured: false, cleared: false },
    };
    expect(isInternalFakeModel(form.model)).toBe(true);
    expect(canSaveModelChannel(form)).toBe(false);
  });

  it("offers vendor models before saving and never exposes fake internals", () => {
    expect(
      availableModelOptions(
        ["gpt-5.5"],
        ["fake-planner", "gpt-custom"],
        "",
      ),
    ).toEqual(["gpt-5.5", "gpt-custom"]);
  });

  it("keeps saved, recommended and account-visible models in distinct groups", () => {
    expect(
      modelOptionGroups(
        ["gpt-5.5", "gpt-shared"],
        ["gpt-shared", "gpt-account", "fake-model"],
        "gpt-5.6-luna",
      ),
    ).toEqual([
      { label: "当前已保存", models: ["gpt-5.6-luna"] },
      { label: "服务商推荐", models: ["gpt-5.5", "gpt-shared"] },
      {
        label: "账号返回（尚未验证兼容性）",
        models: ["gpt-account"],
      },
    ]);
  });

  it("filters a large account directory without hiding saved recommendations", () => {
    expect(
      filterDiscoveredModels(
        ["gpt-5.5", "gpt-4.1-mini", "text-embedding-3-large"],
        "GPT-5",
      ),
    ).toEqual(["gpt-5.5"]);
  });
});
