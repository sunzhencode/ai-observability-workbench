import { describe, expect, it } from "vitest";
import { ApiError } from "./api/client";
import { describeRequestFailure } from "./requestError";

const TITLE = "告警列表暂时不可用；请刷新后重试";

describe("describeRequestFailure", () => {
  it("keeps the caller's title so the message stays specific", () => {
    expect(describeRequestFailure(new ApiError("boom", 500), TITLE).title).toBe(TITLE);
  });

  it("prefers a body the backend wrote for a human", () => {
    const failure = describeRequestFailure(
      new ApiError("Request failed (400)", 400, "地址必须以 http:// 开头"),
      TITLE,
    );
    expect(failure.detail).toBe("地址必须以 http:// 开头");
    expect(failure.retryable).toBe(false);
  });

  it("unwraps FastAPI's detail envelope instead of showing JSON", () => {
    const body = JSON.stringify({ detail: "webhook must be an official Feishu host" });
    expect(describeRequestFailure(new ApiError("x", 400, body), TITLE).detail).toBe(
      "webhook must be an official Feishu host",
    );
  });

  it("unwraps the platform error envelope instead of exposing transport JSON", () => {
    const body = JSON.stringify({
      error: {
        code: "MODEL_PROVIDER_PROFILE_REQUIRED",
        message: "配置当前状态不允许该操作",
        request_id: "req-42",
        details: {},
      },
    });
    expect(describeRequestFailure(new ApiError("x", 409, body), TITLE).detail).toBe(
      "配置当前状态不允许该操作（MODEL_PROVIDER_PROFILE_REQUIRED）",
    );
  });

  it("joins the messages of a pydantic validation list", () => {
    const body = JSON.stringify({
      detail: [
        { loc: ["body", "name"], msg: "field required" },
        { loc: ["body", "url"], msg: "invalid url" },
      ],
    });
    expect(describeRequestFailure(new ApiError("x", 422, body), TITLE).detail).toBe(
      "field required；invalid url",
    );
  });

  it("keeps a body that is not the envelope it expected", () => {
    expect(describeRequestFailure(new ApiError("x", 400, "plain text"), TITLE).detail).toBe(
      "plain text",
    );
    expect(describeRequestFailure(new ApiError("x", 400, "{broken"), TITLE).detail).toBe("{broken");
    expect(describeRequestFailure(new ApiError("x", 400, '{"other":1}'), TITLE).detail).toBe(
      '{"other":1}',
    );
  });

  it("explains a version conflict instead of showing the status code", () => {
    expect(describeRequestFailure(new ApiError("Request failed (409)", 409), TITLE).detail).toBe(
      "配置在你编辑期间被改过，请刷新后重试。",
    );
  });

  it("explains a missing object", () => {
    expect(describeRequestFailure(new ApiError("Request failed (404)", 404), TITLE).detail).toBe(
      "对象不存在，可能已被删除或归档。",
    );
  });

  it("treats server faults as retryable and client faults as not", () => {
    expect(describeRequestFailure(new ApiError("x", 503), TITLE).retryable).toBe(true);
    expect(describeRequestFailure(new ApiError("x", 422), TITLE).retryable).toBe(false);
  });

  it("names the status when there is nothing else to say", () => {
    expect(describeRequestFailure(new ApiError("x", 503), TITLE).detail).toBe("服务端返回 503。");
  });

  it("says the backend is unreachable when fetch never got there", () => {
    const failure = describeRequestFailure(new TypeError("Failed to fetch"), TITLE);
    expect(failure.detail).toBe("无法连接后端，请确认它还在运行。");
    expect(failure.retryable).toBe(true);
  });

  it("passes through a plain Error's message", () => {
    expect(describeRequestFailure(new Error("解析失败"), TITLE).detail).toBe("解析失败");
  });

  it("never invents a detail for a value it cannot read", () => {
    expect(describeRequestFailure("nope", TITLE).detail).toBeNull();
    expect(describeRequestFailure(undefined, TITLE).detail).toBeNull();
    expect(describeRequestFailure(new Error("   "), TITLE).detail).toBeNull();
  });
});
