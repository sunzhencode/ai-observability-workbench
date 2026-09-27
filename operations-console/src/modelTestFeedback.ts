import { modelFailureText } from "./modelFailure";

export type ModelTestAttempt =
  | { state: "pending" }
  | { state: "success" }
  | { state: "failure"; code: string; detail: string }
  | { state: "request-failure"; text: string };

export interface ModelTestFeedback {
  tone: "pending" | "success" | "error";
  text: string;
}

export function modelTestButtonText(
  attempt: ModelTestAttempt | null,
  lastTestCode: string | null,
  fakeMode: boolean,
): string {
  if (attempt?.state === "pending") {
    return fakeMode ? "正在离线测试…" : "正在远程测试…";
  }
  const prefix =
    attempt?.state === "success" || (!attempt && lastTestCode === "OK")
      ? "测试成功 · 再次测试"
      : attempt?.state === "failure" ||
          attempt?.state === "request-failure" ||
          (!attempt && lastTestCode)
        ? "测试未通过 · 重新测试"
        : fakeMode
          ? "离线测试已保存配置"
          : "远程测试当前模型";
  return fakeMode ? prefix : `${prefix}（2 次调用，可能计费）`;
}

function testedAtText(value: string): string | null {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return `${parsed.toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

export function modelTestFeedback(
  attempt: ModelTestAttempt | null,
  model: string,
  lastTestCode: string | null,
  lastTestedAt: string | null,
  fakeMode: boolean,
): ModelTestFeedback | null {
  const mode = fakeMode ? "离线" : "远程";
  if (attempt?.state === "pending") {
    return {
      tone: "pending",
      text: fakeMode
        ? `正在离线测试 ${model}；不会访问填写的地址，请勿重复点击。`
        : `正在远程测试 ${model}；将进行 2 次模型调用，请勿重复点击。`,
    };
  }
  if (attempt?.state === "success") {
    return {
      tone: "success",
      text: `本次${mode}测试成功：${model} 已通过 Planner 与 Analyst 两项结构化契约；启用状态没有改变。`,
    };
  }
  if (attempt?.state === "failure") {
    return {
      tone: "error",
      text: `本次${mode}测试未通过：${modelFailureText(attempt.code, attempt.detail)}；启用状态没有改变。`,
    };
  }
  if (attempt?.state === "request-failure") {
    return {
      tone: "error",
      text: `本次${mode}测试未完成：${attempt.text}；启用状态没有改变。`,
    };
  }
  if (lastTestCode && lastTestedAt) {
    const when = testedAtText(lastTestedAt);
    if (lastTestCode === "OK") {
      return {
        tone: "success",
        text: `最近一次${mode}测试成功${when ? `：${when}` : ""}。`,
      };
    }
    return {
      tone: "error",
      text: `最近一次${mode}测试未通过${when ? `（${when}）` : ""}：${modelFailureText(lastTestCode)}。`,
    };
  }
  return null;
}
