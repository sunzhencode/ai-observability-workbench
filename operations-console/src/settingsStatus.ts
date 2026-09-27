// Pure status/copy logic for 系统设置, kept out of the page so it can be tested
// without a DOM. See PRODUCT_SPEC.md CAP-01/CAP-08.
import type { EventSource, Health, SourcePollHealth } from "./types";

export type Tone = "ok" | "degraded" | "neutral";

export interface SourceStatus {
  label: string;
  /** What to do next, or null when the state needs no action. */
  nextStep: string | null;
  tone: Tone;
}

/**
 * A status that only states a fact leaves the user stranded, so every
 * actionable state carries its next step.
 */
export function sourceStatus(source: EventSource): SourceStatus {
  if (source.lifecycle_state === "ARCHIVED") {
    return { label: "已归档", nextStep: null, tone: "neutral" };
  }
  if (source.lifecycle_state === "DISABLED") {
    return { label: "已停用", nextStep: "可重新启用", tone: "degraded" };
  }
  if (source.status === "CONNECTION_ERROR") {
    return { label: "连接异常", nextStep: "检查地址", tone: "degraded" };
  }
  return { label: "已启用", nextStep: null, tone: "ok" };
}

export function toneClassName(tone: Tone): string {
  if (tone === "ok") return "status-label--ok";
  if (tone === "degraded") return "status-label--degraded";
  return "";
}

export function sourceStatusText(source: EventSource): string {
  const status = sourceStatus(source);
  return status.nextStep ? `${status.label} · ${status.nextStep}` : status.label;
}

export interface PollHealthSummary {
  primary: string;
  secondary: string;
}

/**
 * The old single line packed four facts together and could not be scanned.
 * The partial-success timestamp is only worth showing when it is actually
 * newer than the last complete poll — that is the case that needs attention.
 */
export function pollHealthSummary(
  health: SourcePollHealth | undefined,
  format: (iso: string) => string = (iso) => new Date(iso).toLocaleString(),
): PollHealthSummary {
  if (!health) {
    return { primary: "暂无轮询记录", secondary: "启用后开始采集" };
  }
  const primary = `${health.health} · ${health.endpoint_succeeded}/${health.endpoint_total} endpoint`;
  const complete = health.last_complete_success_at;
  const any = health.last_any_success_at;
  let secondary = complete ? `完整成功 ${format(complete)}` : "尚无完整成功";
  if (any && (!complete || Date.parse(any) > Date.parse(complete))) {
    secondary += ` · 部分成功 ${format(any)}`;
  }
  return { primary, secondary };
}

export interface SourceModeSummary {
  tone: Tone;
  title: string;
  detail: string;
}

/**
 * States what is serving alerts. F21 removed the `.env` fallback, so an empty
 * registry means "nothing configured" and the page must say so with a next step
 * rather than showing a silent empty list.
 */
export function sourceModeSummary(
  health: Health | null,
  managedSourceCount: number,
): SourceModeSummary {
  if (!health) {
    return {
      tone: "neutral",
      title: "正在读取当前生效来源",
      detail: "",
    };
  }
  if (health.source_mode === "REGISTRY") {
    return {
      tone: "ok",
      title: `由 ${managedSourceCount} 个受管来源供数`,
      detail: "告警、Watchdog 与通知都按来源隔离。",
    };
  }
  return {
    tone: "degraded",
    title: "尚未配置任何告警来源",
    detail: "填一个 Alertmanager 名称和地址就能开始；在此之前系统不会轮询任何地址。",
  };
}


/**
 * Turn a backend safe code into something the user can act on.
 *
 * A bare "MASTER_KEY_NOT_CONFIGURED" in a toast tells nobody what to do; the
 * fix lives in a file the UI cannot reach, so the message has to name it.
 */
export function explainSaveError(message: string): string {
  if (message.includes("MASTER_KEY_NOT_CONFIGURED")) {
    return (
      "无法保存凭证：后端读写不了本地主密钥文件 backend/data/master.key。" +
      "检查该目录是否可写后重试——它是解密已保存凭证的唯一钥匙，值得跟数据库一起备份。"
    );
  }
  if (message.includes("SECRET_UNAVAILABLE")) {
    return "已保存的凭证无法解密：主密钥与写入时不一致，请恢复原主密钥或重新输入凭证。";
  }
  if (message.includes("REVISION_CONFLICT")) {
    return "配置已被另一处修改，请刷新后重试。";
  }
  return message;
}
