import { describe, expect, it } from "vitest";
import { modelTestButtonText, modelTestFeedback } from "./modelTestFeedback";

describe("modelTestFeedback", () => {
  it("keeps a long remote test visibly pending next to the action", () => {
    expect(
      modelTestFeedback(
        { state: "pending" },
        "gpt-5.5",
        "OK",
        "2026-09-01T01:02:03Z",
        false,
      ),
    ).toEqual({
      tone: "pending",
      text: "正在远程测试 gpt-5.5；将进行 2 次模型调用，请勿重复点击。",
    });
  });

  it("shows the just-finished result instead of making the user hunt below the fold", () => {
    expect(
      modelTestFeedback(
        { state: "success" },
        "gpt-5.5",
        "OK",
        "2026-09-01T01:02:03Z",
        false,
      ),
    ).toEqual({
      tone: "success",
      text: "本次远程测试成功：gpt-5.5 已通过 Planner 与 Analyst 两项结构化契约；启用状态没有改变。",
    });
    expect(modelTestButtonText({ state: "success" }, "OK", false)).toBe(
      "测试成功 · 再次测试（2 次调用，可能计费）",
    );
  });

  it("restores a persisted recent test result after refresh", () => {
    expect(
      modelTestFeedback(
        null,
        "gpt-5.5",
        "OK",
        "2026-09-01T01:02:03Z",
        false,
      ),
    ).toEqual({
      tone: "success",
      text: "最近一次远程测试成功：2026-09-01 01:02:03 UTC。",
    });
    expect(modelTestButtonText(null, "OK", false)).toBe(
      "测试成功 · 再次测试（2 次调用，可能计费）",
    );
  });

  it("turns a failed contract code into a next action", () => {
    expect(
      modelTestFeedback(
        {
          state: "failure",
          code: "MODEL_ANALYST_CONTRACT_INVALID",
          detail: "",
        },
        "gpt-5.5",
        "MODEL_ANALYST_CONTRACT_INVALID",
        "2026-09-01T01:02:03Z",
        false,
      ),
    ).toEqual({
      tone: "error",
      text: "本次远程测试未通过：模型不能按调查结论协议返回可校验结果——换一个支持严格结构化输出的模型；启用状态没有改变。",
    });
    expect(
      modelTestButtonText(
        {
          state: "failure",
          code: "MODEL_ANALYST_CONTRACT_INVALID",
          detail: "",
        },
        "MODEL_ANALYST_CONTRACT_INVALID",
        false,
      ),
    ).toBe("测试未通过 · 重新测试（2 次调用，可能计费）");
  });
});
