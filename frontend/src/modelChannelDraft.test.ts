import { describe, expect, it } from "vitest";
import {
  availableModelOptions,
  canSaveModelChannel,
  canTestModelChannel,
  emptyModelChannelForm,
  isInternalFakeModel,
  latestModelRevision,
  modelChannelDraft,
  modelChannelFormFrom,
} from "./modelChannelDraft";
import type { ModelChannel } from "./types";

function channel(): ModelChannel {
  return {
    id: 1,
    name: "primary",
    kind: "OPENAI_COMPATIBLE",
    enabled: false,
    active_revision_id: null,
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:00:00Z",
    revisions: [
      {
        id: 3,
        state: "DRAFT",
        base_url: "https://model.example.com/v1",
        base_host: "model.example.com",
        model: "model-a",
        secret_configured: true,
        tested_ok_at: null,
        created_at: "2026-07-31T00:00:00Z",
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
    expect(latestModelRevision(channel())?.id).toBe(3);
  });
});

describe("draft vs activation gates", () => {
  it("saves a draft with no model name, so the picker can supply one", () => {
    // The deadlock this fixes: the picker needs a saved draft to authenticate
    // with (the key never round-trips through the browser), and saving used to
    // demand the very name the picker existed to provide.
    const form = {
      name: "openai",
      baseUrl: "https://api.openai.com/v1",
      model: "",
      apiKey: { input: "k-secret", configured: false, cleared: false },
    };
    expect(canSaveModelChannel(form)).toBe(true);
    expect(canTestModelChannel(form)).toBe(false);
  });

  it("will not test a draft that names no model", () => {
    const form = {
      name: "openai",
      baseUrl: "https://api.openai.com/v1",
      model: "  ",
      apiKey: { input: "", configured: true, cleared: false },
    };
    expect(canTestModelChannel(form)).toBe(false);
  });

  it("is testable once a model is chosen", () => {
    const form = {
      name: "openai",
      baseUrl: "https://api.openai.com/v1",
      model: "gpt-4o",
      apiKey: { input: "", configured: true, cleared: false },
    };
    expect(canTestModelChannel(form)).toBe(true);
  });

  it("still needs a name, an address and a key", () => {
    const base = {
      name: "openai",
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
});
