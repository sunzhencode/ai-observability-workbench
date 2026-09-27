export const INVESTIGATION_START_FEEDBACK_MS = 5_000;

export function investigationStartLabel({
  hasInvestigation,
  isPending = false,
  isCoolingDown = false,
}: {
  hasInvestigation: boolean;
  isPending?: boolean;
  isCoolingDown?: boolean;
}): string {
  if (isPending) return "正在记录告警并读取证据…";
  if (isCoolingDown) return "基础证据已生成";
  return hasInvestigation ? "重新调查当前事件" : "开始证据调查";
}

export function investigationStartSuccessMessage(
  createdAt: string,
  format: (iso: string) => string = (iso) => new Date(iso).toLocaleString(),
): string {
  return `已显示生成于 ${format(createdAt)} 的基础证据；下方已切换到本次结果。`;
}
