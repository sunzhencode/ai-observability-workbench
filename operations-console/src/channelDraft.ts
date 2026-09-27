/**
 * Channel form logic, kept out of the component so it can be tested.
 *
 * The user's complaint that started F24 was that the page did not say what it
 * wanted: they asked where to configure the "Feishu token", and the answer is
 * that there is no token field -- it is the last path segment of the webhook.
 * The guidance below is part of the contract, not decoration, so it lives here
 * with the shapes rather than being scattered through JSX.
 */
import type {
  ChannelConfigDraft,
  ChannelKind,
  NotificationChannel,
  NotificationEventType,
  SecretUpdate,
} from "./types";

export const CHANNEL_KINDS: readonly ChannelKind[] = [
  "FEISHU_CUSTOM_BOT",
  "SMTP",
  "GENERIC_WEBHOOK",
];

export function notificationDataDisclosure(kind: ChannelKind): string[] {
  return [
    `${KIND_LABELS[kind]}会收到告警标题、来源、严重度、成员摘要，以及当时已有的最多 2 条确定性指标值。`,
    "消息可能包含工作台 Incident 深链和最多 2 个 Grafana 面板深链；不会临时查询指标，也不会发送完整时序点位。",
    "告警 label 可能含主机名、namespace 和集群名；只配置你掌握的固定目标。",
  ];
}

const EVENTS: NotificationEventType[] = [
  "FIRING_OPENED",
  "SEVERITY_ESCALATED",
  "REMINDER",
  "RECOVERED",
];

const CLEARED: SecretUpdate = { action: "CLEAR" };

type ChannelRevisionSecretSummary = {
  config?: Record<string, unknown>;
  secret_configured?: Record<string, boolean>;
  webhook_configured?: boolean;
  signing_secret_configured?: boolean;
  config_summary?: { secret_configured?: boolean; detail?: string };
} | null;

type SerializedChannelConfig<T> = {
  provider: ChannelKind;
  config: Record<string, unknown>;
  secrets: Record<string, T>;
};

type ChannelKindDefinition = {
  label: string;
  hint: string;
  requiredSecrets: readonly string[];
  empty: () => ChannelConfigDraft;
  storedConfig: (revision: ChannelRevisionSecretSummary) => ChannelConfigDraft;
  withSecrets: (
    config: ChannelConfigDraft,
    read: (field: string) => SecretUpdate,
  ) => ChannelConfigDraft;
  storedSecrets: (revision: ChannelRevisionSecretSummary) => Record<string, boolean>;
  serialize: <T>(
    config: ChannelConfigDraft,
    mapSecret: (secret: SecretUpdate) => T,
  ) => SerializedChannelConfig<T>;
};

function storedRecord(revision: ChannelRevisionSecretSummary): Record<string, unknown> {
  return revision?.config ?? {};
}

function storedText(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function storedBooleanMap(
  value: unknown,
  defaults: Record<NotificationEventType, boolean>,
): Record<NotificationEventType, boolean> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return defaults;
  const record = value as Record<string, unknown>;
  return Object.fromEntries(
    EVENTS.map((event) => [event, typeof record[event] === "boolean" ? record[event] : defaults[event]]),
  ) as Record<NotificationEventType, boolean>;
}

function exactSecret(
  revision: ChannelRevisionSecretSummary,
  field: string,
): boolean | undefined {
  const value = revision?.secret_configured?.[field];
  return typeof value === "boolean" ? value : undefined;
}

function configFor<K extends ChannelKind>(
  config: ChannelConfigDraft,
  kind: K,
): Extract<ChannelConfigDraft, { kind: K }> {
  if (config.kind !== kind) throw new Error("CHANNEL_KIND_CONFIG_MISMATCH");
  return config as Extract<ChannelConfigDraft, { kind: K }>;
}

export const CHANNEL_KIND_REGISTRY: Record<ChannelKind, ChannelKindDefinition> = {
  FEISHU_CUSTOM_BOT: {
    label: "飞书自定义机器人",
    hint: "一个通道对应一个飞书群。地址在：群设置 → 群机器人 → 添加自定义机器人。",
    requiredSecrets: ["webhook"],
    empty: () => ({
      kind: "FEISHU_CUSTOM_BOT",
      webhook: { action: "REPLACE", value: "" },
      signing_secret: CLEARED,
      required_keyword: null,
      mention_mode: "NONE",
      mention_users: [],
      mention_on: Object.fromEntries(EVENTS.map((event) => [event, false])) as Record<
        NotificationEventType,
        boolean
      >,
    }),
    storedConfig: (revision) => {
      const base = CHANNEL_KIND_REGISTRY.FEISHU_CUSTOM_BOT.empty();
      if (base.kind !== "FEISHU_CUSTOM_BOT") throw new Error("CHANNEL_KIND_CONFIG_MISMATCH");
      const config = storedRecord(revision);
      const mentionMode = config.mention_mode;
      const mentionUsers = Array.isArray(config.mention_users)
        ? config.mention_users.flatMap((item) => {
            if (typeof item !== "object" || item === null || Array.isArray(item)) return [];
            const openId = (item as Record<string, unknown>).open_id;
            return typeof openId === "string" ? [{ open_id: openId }] : [];
          })
        : base.mention_users;
      return {
        ...base,
        required_keyword: typeof config.required_keyword === "string"
          ? config.required_keyword
          : null,
        mention_mode: mentionMode === "USERS" || mentionMode === "ALL" || mentionMode === "NONE"
          ? mentionMode
          : base.mention_mode,
        mention_users: mentionUsers,
        mention_on: storedBooleanMap(config.mention_on, base.mention_on),
      };
    },
    withSecrets: (config, read) => ({
      ...configFor(config, "FEISHU_CUSTOM_BOT"),
      webhook: read("webhook"),
      signing_secret: read("signing_secret"),
    }),
    storedSecrets: (revision) => ({
      webhook: exactSecret(revision, "webhook") ?? Boolean(revision?.webhook_configured),
      signing_secret: exactSecret(revision, "signing_secret")
        ?? Boolean(revision?.signing_secret_configured),
    }),
    serialize: (config, mapSecret) => {
      const value = configFor(config, "FEISHU_CUSTOM_BOT");
      return {
        provider: value.kind,
        config: {
          required_keyword: value.required_keyword,
          mention_mode: value.mention_mode,
          mention_users: value.mention_users,
          mention_on: value.mention_on,
        },
        secrets: {
          webhook: mapSecret(value.webhook),
          signing_secret: mapSecret(value.signing_secret),
        },
      };
    },
  },
  SMTP: {
    label: "邮件（SMTP）",
    hint: "一个通道对应一组固定收件人。需要邮箱服务商提供的 SMTP 主机、端口和账号。",
    requiredSecrets: [],
    empty: () => ({
      kind: "SMTP",
      host: "",
      port: 587,
      tls_mode: "STARTTLS",
      username: "",
      password: { action: "REPLACE", value: "" },
      from_addr: "",
      to_addrs: [],
      subject_prefix: "",
    }),
    storedConfig: (revision) => {
      const base = CHANNEL_KIND_REGISTRY.SMTP.empty();
      if (base.kind !== "SMTP") throw new Error("CHANNEL_KIND_CONFIG_MISMATCH");
      const config = storedRecord(revision);
      const toAddrs = Array.isArray(config.to_addrs)
        ? config.to_addrs.filter((item): item is string => typeof item === "string")
        : base.to_addrs;
      return {
        ...base,
        host: storedText(config.host),
        port: config.port === 465 || config.port === 587 ? config.port : base.port,
        tls_mode: config.tls_mode === "TLS" || config.tls_mode === "STARTTLS"
          ? config.tls_mode
          : base.tls_mode,
        username: storedText(config.username),
        from_addr: storedText(config.from_addr),
        to_addrs: toAddrs,
        subject_prefix: storedText(config.subject_prefix),
      };
    },
    withSecrets: (config, read) => ({
      ...configFor(config, "SMTP"),
      password: read("password"),
    }),
    storedSecrets: (revision) => ({
      password: exactSecret(revision, "password")
        ?? Boolean(revision?.config_summary?.secret_configured),
    }),
    serialize: (config, mapSecret) => {
      const value = configFor(config, "SMTP");
      return {
        provider: value.kind,
        config: {
          host: value.host,
          port: value.port,
          tls_mode: value.tls_mode,
          username: value.username,
          from_addr: value.from_addr,
          to_addrs: value.to_addrs,
          subject_prefix: value.subject_prefix,
        },
        secrets: { password: mapSecret(value.password) },
      };
    },
  },
  GENERIC_WEBHOOK: {
    label: "通用 Webhook",
    hint: "把事件 POST 到你自己的 HTTPS 端点，由你的服务决定后续处理。",
    requiredSecrets: [],
    empty: () => ({
      kind: "GENERIC_WEBHOOK",
      url: "",
      headers: CLEARED,
      signing_secret: CLEARED,
      timeout_seconds: 8,
    }),
    storedConfig: (revision) => {
      const base = CHANNEL_KIND_REGISTRY.GENERIC_WEBHOOK.empty();
      if (base.kind !== "GENERIC_WEBHOOK") throw new Error("CHANNEL_KIND_CONFIG_MISMATCH");
      const config = storedRecord(revision);
      const timeout = config.timeout_seconds;
      return {
        ...base,
        url: storedText(config.url),
        timeout_seconds: typeof timeout === "number" && timeout >= 1 && timeout <= 30
          ? timeout
          : base.timeout_seconds,
      };
    },
    withSecrets: (config, read) => ({
      ...configFor(config, "GENERIC_WEBHOOK"),
      headers: read("headers"),
      signing_secret: read("signing_secret"),
    }),
    storedSecrets: (revision) => ({
      headers: exactSecret(revision, "headers")
        ?? Boolean(revision?.config_summary?.detail?.includes("已配置")),
      signing_secret: exactSecret(revision, "signing_secret")
        ?? Boolean(revision?.config_summary?.secret_configured),
    }),
    serialize: (config, mapSecret) => {
      const value = configFor(config, "GENERIC_WEBHOOK");
      return {
        provider: value.kind,
        config: { url: value.url, timeout_seconds: value.timeout_seconds },
        secrets: {
          headers: mapSecret(value.headers),
          signing_secret: mapSecret(value.signing_secret),
        },
      };
    },
  },
};

export const KIND_LABELS = Object.fromEntries(
  CHANNEL_KINDS.map((kind) => [kind, CHANNEL_KIND_REGISTRY[kind].label]),
) as Record<ChannelKind, string>;

export const KIND_HINTS = Object.fromEntries(
  CHANNEL_KINDS.map((kind) => [kind, CHANNEL_KIND_REGISTRY[kind].hint]),
) as Record<ChannelKind, string>;

/** A blank configuration of the requested kind. */
export function emptyConfig(kind: ChannelKind): ChannelConfigDraft {
  return CHANNEL_KIND_REGISTRY[kind].empty();
}

/** Restore the complete non-secret draft returned by the API. */
export function storedChannelConfig(
  kind: ChannelKind,
  revision: ChannelRevisionSecretSummary,
): ChannelConfigDraft {
  return CHANNEL_KIND_REGISTRY[kind].storedConfig(revision);
}

export function applyChannelSecrets(
  config: ChannelConfigDraft,
  read: (field: string) => SecretUpdate,
): ChannelConfigDraft {
  return CHANNEL_KIND_REGISTRY[config.kind].withSecrets(config, read);
}

export function requiredChannelSecrets(kind: ChannelKind): readonly string[] {
  return CHANNEL_KIND_REGISTRY[kind].requiredSecrets;
}

export function storedChannelSecrets(
  kind: ChannelKind,
  revision: ChannelRevisionSecretSummary,
): Record<string, boolean> {
  return CHANNEL_KIND_REGISTRY[kind].storedSecrets(revision);
}

export function serializeChannelConfig<T>(
  config: ChannelConfigDraft,
  mapSecret: (secret: SecretUpdate) => T,
): SerializedChannelConfig<T> {
  return CHANNEL_KIND_REGISTRY[config.kind].serialize(config, mapSecret);
}

/**
 * 587 pairs with STARTTLS and 465 with implicit TLS; the backend refuses any
 * other combination, so the form keeps them in step instead of letting the user
 * discover it on save.
 */
export function portForTlsMode(mode: "STARTTLS" | "TLS"): number {
  return mode === "TLS" ? 465 : 587;
}

export function tlsModeForPort(port: number): "STARTTLS" | "TLS" {
  return port === 465 ? "TLS" : "STARTTLS";
}

/**
 * Whether the non-secret half of the form is complete.
 *
 * Secrets are deliberately not judged here: a saved channel's credential sits
 * behind an empty box that means "leave it alone", so reading the draft value
 * would make an untouched channel unsavable. `secretFieldSatisfied` covers the
 * required ones. A connection test is separate again -- it is never a gate on
 * saving.
 */
export function canSaveChannel(name: string, config: ChannelConfigDraft): boolean {
  if (name.trim() === "") return false;
  if (config.kind === "SMTP") {
    return (
      config.host.trim() !== ""
      && config.from_addr.includes("@")
      && config.to_addrs.length > 0
      && config.to_addrs.every((item) => item.includes("@"))
    );
  }
  if (config.kind === "GENERIC_WEBHOOK") {
    return config.url.startsWith("https://");
  }
  return true;
}

/** Address list from a textarea: one per line or comma separated. */
export function parseRecipients(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\n,;]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

/**
 * Why a save would be refused, in the user's terms, before they try.
 *
 * The backend enforces all of this; saying it here is what turns "webhook must
 * be an official HTTPS Feishu/Lark V2 bot URL" into something actionable.
 */
export function describeRequirements(config: ChannelConfigDraft): string[] {
  if (config.kind === "SMTP") {
    return [
      "端口 587 用 STARTTLS，端口 465 用隐式 TLS；其他组合会被拒绝。",
      "发件人和收件人都必须是完整邮件地址。",
      "服务器若不支持 STARTTLS，本工具会拒绝发送而不是明文送出密码。",
    ];
  }
  if (config.kind === "GENERIC_WEBHOOK") {
    return [
      "必须是 https:// 且端口 443；不跟随跳转。",
      "解析后指向内网、回环或云元数据地址（如 169.254.169.254）会被拒绝，每次发送都会重新校验。",
      "自定义请求头整体按凭证处理，保存后不回显。",
    ];
  }
  return [
    "地址形如 https://open.feishu.cn/open-apis/bot/v2/hook/<token>——token 就是最后那一段，没有单独的输入框。",
    "「安全关键词」对应飞书机器人的「自定义关键词」，「签名密钥」对应「签名校验」，二选一即可。",
    "@ 对象只在下方勾选的事件上生效；一个事件都不勾就不会 @ 任何人。",
  ];
}

/** A short line describing what a saved channel points at, for the list. */
export function channelTargetSummary(channel: NotificationChannel): string {
  const revision = [...channel.revisions].sort((a, b) => b.version - a.version)[0];
  if (!revision) return "尚未配置";
  const summary = revision.config_summary;
  return summary ? `${summary.target} · ${summary.detail}` : "尚未配置";
}
