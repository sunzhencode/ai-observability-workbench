import type {
  ModelChannel,
  ModelChannelDraft,
  ModelChannelRevision,
} from "./types";
import {
  emptySecretField,
  secretFieldSatisfied,
  secretUpdateFor,
  type SecretFieldState,
} from "./secretField";

export interface ModelChannelForm {
  name: string;
  baseUrl: string;
  model: string;
  apiKey: SecretFieldState;
}

export function latestModelRevision(
  channel: ModelChannel,
): ModelChannelRevision | null {
  return channel.revisions[0] ?? null;
}

/**
 * Prefilled because OpenAI itself is the overwhelmingly common case, and an
 * empty field is one more thing to look up and mistype. Still editable — the
 * kind is OPENAI_**COMPATIBLE**, and plenty of services speak it.
 */
export const DEFAULT_MODEL_BASE_URL = "https://api.openai.com/v1";

const INTERNAL_FAKE_MODELS = new Set(["fake-planner", "fake-model"]);

export function isInternalFakeModel(model: string): boolean {
  return INTERNAL_FAKE_MODELS.has(model.trim().toLowerCase());
}

/** Vendor recommendations are available before save; a real `/models` read only adds choices. */
export function availableModelOptions(
  recommended: readonly string[],
  discovered: readonly string[],
  current: string,
): string[] {
  return [...new Set([current, ...recommended, ...discovered].map((item) => item.trim()))]
    .filter((item) => item.length > 0 && !isInternalFakeModel(item));
}

/** The vendor whose preset a saved address matches, or CUSTOM. */
export function vendorForBaseUrl(
  baseUrl: string,
  vendors: readonly { id: string; base_url: string }[],
): string {
  const trimmed = baseUrl.trim();
  return vendors.find((item) => item.base_url && item.base_url === trimmed)?.id ?? "CUSTOM";
}

export function emptyModelChannelForm(): ModelChannelForm {
  return {
    name: "",
    baseUrl: DEFAULT_MODEL_BASE_URL,
    model: "",
    apiKey: emptySecretField(false),
  };
}

export function modelChannelFormFrom(channel: ModelChannel): ModelChannelForm {
  const revision = latestModelRevision(channel);
  return {
    name: channel.name,
    baseUrl: revision?.base_url ?? "",
    model: revision?.model ?? "",
    apiKey: emptySecretField(revision?.secret_configured ?? false),
  };
}

/**
 * Enough to save a draft — deliberately **not** enough to activate one.
 *
 * The model name is absent on purpose. Requiring it here deadlocked the flow:
 * the picker that supplies the name needs a saved draft to authenticate with,
 * and saving demanded the name. A draft is a work in progress; the gate is
 * `canActivateModelChannel`.
 */
export function canSaveModelChannel(form: ModelChannelForm): boolean {
  return (
    form.name.trim().length > 0
    && form.baseUrl.trim().length > 0
    && !isInternalFakeModel(form.model)
    && secretFieldSatisfied(form.apiKey, true)
  );
}

export function modelChannelDraft(form: ModelChannelForm): ModelChannelDraft {
  return {
    name: form.name.trim(),
    config: {
      kind: "OPENAI_COMPATIBLE",
      base_url: form.baseUrl.trim(),
      model: form.model.trim(),
      api_key: secretUpdateFor(form.apiKey),
    },
  };
}


/** A draft is only testable once it names a model to call. */
export function canTestModelChannel(form: ModelChannelForm): boolean {
  return (
    canSaveModelChannel(form)
    && form.model.trim().length > 0
    && !isInternalFakeModel(form.model)
  );
}
