import type {
  ModelChannel,
  ModelChannelDraft,
  ModelChannelRevision,
  ModelProviderId,
} from "./types";
import {
  emptySecretField,
  secretFieldSatisfied,
  secretUpdateFor,
  type SecretFieldState,
} from "./secretField";

export interface ModelChannelForm {
  name: string;
  providerId: ModelProviderId;
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

export interface ModelOptionGroup {
  label: string;
  models: string[];
}

export function filterDiscoveredModels(
  models: readonly string[],
  query: string,
): string[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...models];
  return models.filter((model) => model.toLowerCase().includes(needle));
}

/**
 * Keep provenance visible: a provider recommendation, an account-visible ID,
 * and the currently saved value mean different things to the operator.
 */
export function modelOptionGroups(
  recommended: readonly string[],
  discovered: readonly string[],
  current: string,
): ModelOptionGroup[] {
  const clean = (items: readonly string[]) =>
    [...new Set(items.map((item) => item.trim()))]
      .filter((item) => item.length > 0 && !isInternalFakeModel(item));
  const currentModels = clean([current]);
  const currentSet = new Set(currentModels);
  const recommendedModels = clean(recommended)
    .filter((item) => !currentSet.has(item));
  const known = new Set([...currentModels, ...recommendedModels]);
  const discoveredModels = clean(discovered)
    .filter((item) => !known.has(item));

  return [
    { label: "当前已保存", models: currentModels },
    { label: "服务商推荐", models: recommendedModels },
    { label: "账号返回（尚未验证兼容性）", models: discoveredModels },
  ].filter((group) => group.models.length > 0);
}

export function emptyModelChannelForm(): ModelChannelForm {
  return {
    name: "",
    providerId: "OPENAI",
    baseUrl: DEFAULT_MODEL_BASE_URL,
    model: "",
    apiKey: emptySecretField(false),
  };
}

export function modelChannelFormFrom(channel: ModelChannel): ModelChannelForm {
  const revision = latestModelRevision(channel);
  return {
    name: channel.name,
    providerId: revision?.provider_id ?? "CUSTOM",
    baseUrl: revision?.base_url ?? "",
    model: revision?.model ?? "",
    apiKey: emptySecretField(revision?.secret_configured ?? false),
  };
}

/** Saving immediately enables this destination, so every runtime field is required. */
export function canSaveModelChannel(form: ModelChannelForm): boolean {
  return (
    form.name.trim().length > 0
    && form.providerId.length > 0
    && form.baseUrl.trim().length > 0
    && form.model.trim().length > 0
    && !isInternalFakeModel(form.model)
    && secretFieldSatisfied(form.apiKey, true)
  );
}

export function modelChannelDraft(form: ModelChannelForm): ModelChannelDraft {
  return {
    name: form.name.trim(),
    config: {
      kind: "OPENAI_COMPATIBLE",
      provider_id: form.providerId,
      base_url: form.baseUrl.trim(),
      model: form.model.trim(),
      api_key: secretUpdateFor(form.apiKey),
    },
  };
}
