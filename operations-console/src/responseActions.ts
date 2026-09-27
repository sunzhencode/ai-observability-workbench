import type { OperationalSignalState, ResolutionCode } from "./types";

export const RESOLUTION_LABELS: Record<ResolutionCode, string> = {
  FIXED: "人工处理后恢复",
  SELF_RECOVERED: "上游自行恢复",
  FALSE_POSITIVE: "确认误报",
  DUPLICATE: "重复事件",
  NO_ACTION: "无需继续处理",
};

const NON_RECOVERED_CODES: readonly ResolutionCode[] = [
  "FALSE_POSITIVE",
  "DUPLICATE",
  "NO_ACTION",
];

export function availableResolutionCodes(
  signal: OperationalSignalState,
): readonly ResolutionCode[] {
  return signal === "RECOVERED"
    ? ["FIXED", "SELF_RECOVERED", ...NON_RECOVERED_CODES]
    : NON_RECOVERED_CODES;
}

export function resolutionReasonRequired(
  signal: OperationalSignalState,
): boolean {
  return signal !== "RECOVERED";
}

export function resolutionHelp(signal: OperationalSignalState): string {
  if (signal === "RECOVERED") {
    return "上游信号已恢复。请选择最符合事实的结束分类；平台不会改写上游状态。";
  }
  return "当前信号尚未确认恢复，因此不能选择“人工处理后恢复”或“上游自行恢复”。如确认误报、重复或无需继续，请填写判断说明。";
}
