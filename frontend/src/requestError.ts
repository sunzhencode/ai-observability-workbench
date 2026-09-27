/**
 * One shape for "the request failed", so the four pages can stop inventing
 * their own.
 *
 * Before F23 a failure was rendered three different ways, and the worst of them
 * — the alert list — caught the error, threw the reason away and kept only a
 * boolean. `PRODUCT_SPEC.md` CAP-08 asks for readable failures; this module is
 * where "readable" is decided, as a pure function, once.
 */
import { ApiError } from "./api/client";

export interface RequestFailure {
  /** Short line for the user: what did not happen. */
  title: string;
  /** The reason, when the server or the browser gave one worth showing. */
  detail: string | null;
  /** Whether trying the same thing again could plausibly work. */
  retryable: boolean;
}

/** Statuses where a second attempt is pointless without changing the request. */
function isClientFault(status: number): boolean {
  return status >= 400 && status < 500;
}

/**
 * FastAPI reports errors as `{"detail": ...}`. Showing the envelope makes the
 * user read JSON to find the sentence inside it, so unwrap when we can and fall
 * back to the raw text when the body is not what we expected.
 */
function unwrapDetail(body: string): string {
  if (!body.startsWith("{") && !body.startsWith("[")) return body;
  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed === "string") return parsed;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      if (typeof detail === "string" && detail.trim() !== "") return detail;
      // Pydantic validation errors are a list of objects; their `msg` fields
      // are the only part worth showing.
      if (Array.isArray(detail)) {
        const messages = detail
          .map((item) =>
            item && typeof item === "object" && typeof (item as { msg?: unknown }).msg === "string"
              ? (item as { msg: string }).msg
              : null,
          )
          .filter((msg): msg is string => msg !== null);
        if (messages.length > 0) return messages.join("；");
      }
    }
  } catch {
    // Not JSON after all; the raw text is still better than nothing.
  }
  return body;
}

function detailFor(cause: ApiError): string | null {
  // A body the backend wrote for a human (explainSaveError et al.) beats the
  // generic message this client builds from the status code.
  const body = cause.body?.trim();
  if (body) return unwrapDetail(body);
  if (cause.status === null) return null;
  if (cause.status === 409) return "配置在你编辑期间被改过，请刷新后重试。";
  if (cause.status === 404) return "对象不存在，可能已被删除或归档。";
  return `服务端返回 ${cause.status}。`;
}

/**
 * Describe any thrown value.
 *
 * `fallbackTitle` is what the caller was trying to do ("告警列表加载失败"), so
 * the title stays specific even when the cause carries nothing readable.
 */
export function describeRequestFailure(cause: unknown, fallbackTitle: string): RequestFailure {
  if (cause instanceof ApiError) {
    return {
      title: fallbackTitle,
      detail: detailFor(cause),
      retryable: cause.status === null || !isClientFault(cause.status),
    };
  }
  if (cause instanceof TypeError) {
    // fetch rejects with a TypeError when it never reached the server.
    return { title: fallbackTitle, detail: "无法连接后端，请确认它还在运行。", retryable: true };
  }
  if (cause instanceof Error && cause.message.trim() !== "") {
    return { title: fallbackTitle, detail: cause.message.trim(), retryable: true };
  }
  return { title: fallbackTitle, detail: null, retryable: true };
}

/**
 * Title and reason on one line, for the places that have room for a sentence
 * rather than a panel: an inline status line under a form.
 */
export function requestFailureText(cause: unknown, fallbackTitle: string): string {
  const failure = describeRequestFailure(cause, fallbackTitle);
  return failure.detail === null ? failure.title : `${failure.title}：${failure.detail}`;
}
