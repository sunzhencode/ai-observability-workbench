/**
 * "Let AI look at this alert" — hypotheses, what they rest on, what to do next.
 *
 * The rendering is doing one job the wording cannot: **it must be impossible to
 * read a conclusion without seeing what it rests on.** Every hypothesis shows
 * its cited facts inline, because a claim you cannot check reads exactly as well
 * as one you can, and that is the failure mode this whole structure exists to
 * prevent (D22).
 *
 * Two things it deliberately does not do:
 *
 * - **Never render unstructured model text** (D44). When validation fails the
 *   page says so and shows nothing else. A fluent paragraph labelled "not
 *   structured" is still a fluent paragraph, and people read it.
 * - **Never run on its own** (D13). No effect, no refetch, no auto-trigger on
 *   selection. It costs money.
 */
import { useState } from "react";

import { modelFailureText } from "../../modelFailure";
import { useInvestigateAlert } from "../../queries/metrics";
import type { Hypothesis, Investigation } from "../../types";

interface Props {
  alertId: number;
}

/** Each verdict sends the reader somewhere different — that is why there are four. */
const VERDICT: Record<
  Hypothesis["verdict"],
  { label: string; tone: string; help: string }
> = {
  SUPPORTED: {
    label: "证据支持",
    tone: "ok",
    // Not "root cause": correlated metrics and past handling cannot establish
    // causation, and overclaiming is what the four verdicts exist to stop (D43).
    help: "有证据支持，但这不等于已确定因果",
  },
  SYMPTOM: { label: "是症状", tone: "warn", help: "确实发生了，但更像是结果而不是原因" },
  DISPROVEN: { label: "已排除", tone: "muted", help: "查过了，证据不支持——省掉同一条死路" },
  BLOCKED: { label: "查不下去", tone: "muted", help: "有道理，但现有证据判不了" },
};

const FAILURE_TEXT: Record<string, string> = {
  MODEL_NOT_CONFIGURED: "还没有可用的模型服务。去「系统设置 → 模型服务」配一个",
  NO_EVIDENCE:
    "这条告警一条曲线都没画出来，没有可依据的事实。先把上面的指标问题解决，再来调查——" +
    "否则模型只能复述你自己写的告警文案",
  FAILED_VALIDATION:
    "模型两次都没能给出带证据归属的结论。换一个能力更强的模型试试；" +
    "它这次说了什么不会展示——没有归属的结论正是这里要挡住的东西",
};

function factText(result: Investigation, id: string): string {
  return result.facts.find((item) => item.fact_id === id)?.statement ?? id;
}

export function AlertInvestigation({ alertId }: Props) {
  const investigate = useInvestigateAlert();
  const [result, setResult] = useState<Investigation | null>(null);

  const run = () => {
    setResult(null);
    investigate.mutate(alertId, { onSuccess: setResult });
  };

  return (
    <section className="card alert-investigation" aria-label="AI 调查">
      <header>
        <h4>让 AI 看看这条告警</h4>
        <p className="dim">
          模型会读<strong>告警内容</strong>、<strong>上面这些曲线的统计</strong>和
          <strong>这个组过去怎么被处理的</strong>，给出假设、依据和下一步建议。
          建议由你执行，它不动手。会调用模型服务，<strong>可能计费</strong>。
        </p>
      </header>

      <button
        type="button"
        className="secondary-btn"
        disabled={investigate.isPending}
        onClick={run}
      >
        {investigate.isPending ? "模型正在读证据…" : "让 AI 看看"}
      </button>

      {result?.failure ? (
        <p className="alert-note" role="status">
          {FAILURE_TEXT[result.failure] ?? modelFailureText(result.failure)}
          {result.possibly_billed ? (
            <span className="dim"> · 这次请求可能已经产生费用</span>
          ) : null}
        </p>
      ) : null}

      {result && !result.failure ? (
        <>
          <p className="dim alert-investigation__meta">
            {result.model_name} · {result.model_calls} 次调用 · {result.prompt_version}
          </p>
          <ol className="alert-investigation__list">
            {result.hypotheses.map((item, index) => {
              const verdict = VERDICT[item.verdict];
              return (
                <li key={`${item.statement}-${index}`}>
                  <div className="alert-investigation__head">
                    <span className={`pill pill--${verdict.tone}`}>{verdict.label}</span>
                    <strong>{item.statement}</strong>
                  </div>
                  <p className="dim">{verdict.help}</p>

                  {/* Inline, always. A conclusion whose evidence is a click away
                      is a conclusion most people will take on trust. */}
                  {item.supporting_fact_ids.length > 0 ? (
                    <ul className="alert-investigation__facts">
                      {item.supporting_fact_ids.map((id) => (
                        <li key={id}>
                          <span className="dim">依据</span> {factText(result, id)}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {item.contradicting_fact_ids.length > 0 ? (
                    <ul className="alert-investigation__facts">
                      {item.contradicting_fact_ids.map((id) => (
                        <li key={id}>
                          <span className="dim">反证</span> {factText(result, id)}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {item.missing_evidence.length > 0 ? (
                    <p className="dim">
                      还缺：{item.missing_evidence.join("；")}
                    </p>
                  ) : null}

                  {item.recommendations.length > 0 ? (
                    <ul className="alert-investigation__actions">
                      {item.recommendations.map((rec, position) => (
                        <li key={`${rec.text}-${position}`}>
                          <span className="pill pill--muted">
                            {rec.kind === "MITIGATION_CANDIDATE" ? "可能的处置" : "下一步查"}
                          </span>{" "}
                          {rec.text}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              );
            })}
          </ol>
          {result.hypotheses.length === 0 ? (
            <p className="dim">模型这次没能给出有证据支撑的判断。</p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
