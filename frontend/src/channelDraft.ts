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

export const KIND_LABELS: Record<ChannelKind, string> = {
  FEISHU_CUSTOM_BOT: "飞书自定义机器人",
  SMTP: "邮件（SMTP）",
  GENERIC_WEBHOOK: "通用 Webhook",
};

export const KIND_HINTS: Record<ChannelKind, string> = {
  FEISHU_CUSTOM_BOT: "一个通道对应一个飞书群。地址在：群设置 → 群机器人 → 添加自定义机器人。",
  SMTP: "一个通道对应一组固定收件人。需要邮箱服务商提供的 SMTP 主机、端口和账号。",
  GENERIC_WEBHOOK: "把事件 POST 到你自己的 HTTPS 端点，由你的服务决定后续处理。",
};

const EVENTS: NotificationEventType[] = [
  "FIRING_OPENED",
  "SEVERITY_ESCALATED",
  "REMINDER",
  "RECOVERED",
];

const CLEARED: SecretUpdate = { action: "CLEAR" };

/** A blank configuration of the requested kind. */
export function emptyConfig(kind: ChannelKind): ChannelConfigDraft {
  if (kind === "SMTP") {
    return {
      kind: "SMTP",
      host: "",
      port: 587,
      tls_mode: "STARTTLS",
      username: "",
      password: { action: "REPLACE", value: "" },
      from_addr: "",
      to_addrs: [],
      subject_prefix: "",
    };
  }
  if (kind === "GENERIC_WEBHOOK") {
    return {
      kind: "GENERIC_WEBHOOK",
      url: "",
      headers: CLEARED,
      signing_secret: CLEARED,
      timeout_seconds: 8,
    };
  }
  return {
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
  };
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
