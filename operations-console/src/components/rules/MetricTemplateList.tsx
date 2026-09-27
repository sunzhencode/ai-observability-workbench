/** F27 template catalogue plus F28 source scope. */
import { useState } from "react";
import { useEventSources } from "../../queries/catalog";
import {
  useDuplicateMetricTemplate,
  useMetricTemplates,
  useUpdateMetricTemplate,
} from "../../queries/metrics";
import { describeRequestFailure } from "../../requestError";
import type { EventSource, MetricTemplate, SourceScope } from "../../types";

function mutationFailure(error: unknown, fallback: string) {
  if (!error) return null;
  const failure = describeRequestFailure(error, fallback);
  return `${failure.title}：${failure.detail}`;
}

function sourceScopeText(scope: SourceScope, sources: readonly EventSource[]) {
  if (scope.mode === "ALL") return "全部来源";
  const names = scope.source_ids.map(
    (id) => sources.find((source) => source.id === id)?.name || id,
  );
  return names.length > 0 ? names.join(" / ") : "尚未选择来源";
}

function MetricTemplateRow({
  template,
  sources,
}: {
  template: MetricTemplate;
  sources: readonly EventSource[];
}) {
  const update = useUpdateMetricTemplate();
  const duplicate = useDuplicateMetricTemplate();
  const [editing, setEditing] = useState(false);

  const pending = update.isPending || duplicate.isPending;
  const error =
    mutationFailure(update.error, "保存模板失败") ||
    mutationFailure(duplicate.error, "复制模板失败");

  const saveScope = (sourceScope: SourceScope) => {
    update.mutate({ templateId: template.id, body: { source_scope: sourceScope } });
  };

  const selectMode = (mode: SourceScope["mode"]) => {
    if (mode === "ALL") {
      saveScope({ mode: "ALL", source_ids: [] });
      return;
    }
    const first = template.origin?.source_id || sources[0]?.id;
    if (first) saveScope({ mode: "SELECTED", source_ids: [first] });
  };

  const toggleSource = (sourceId: string, checked: boolean) => {
    const current = new Set(template.source_scope.source_ids);
    if (checked) current.add(sourceId);
    else current.delete(sourceId);
    if (current.size === 0) return; // SELECTED is never an empty backdoor to ALL.
    saveScope({ mode: "SELECTED", source_ids: [...current] });
  };

  return (
    <>
      <tr
        data-builtin={template.builtin_key ? "1" : "0"}
        data-testid="template-row"
      >
        <td>
          <label className="metric-template-table__toggle">
            <input
              type="checkbox"
              checked={template.enabled}
              disabled={pending}
              onChange={(event) =>
                update.mutate({
                  templateId: template.id,
                  body: { enabled: event.target.checked },
                })
              }
            />
            <span className="sr-only">
              {template.enabled ? "已启用" : "未启用"} {template.name}
            </span>
          </label>
        </td>
        <td>
          <div>{template.name}</div>
          <div className="dim">{template.description}</div>
          {template.user_modified ? <div className="dim">已被你改过，版本升级不会覆盖</div> : null}
          {template.origin ? (
            <div className="dim" data-testid="template-origin">
              来自 Grafana · {template.origin.dashboard_title}
            </div>
          ) : null}
          <div className="metric-template-table__actions">
            <button type="button" className="link" onClick={() => setEditing((value) => !value)}>
              {editing ? "收起设置" : "设置适用范围"}
            </button>
            <button type="button" className="link" disabled={pending} onClick={() => duplicate.mutate(template)}>
              复制
            </button>
          </div>
        </td>
        <td>
          <input
            type="number"
            className="metric-template-table__priority"
            value={template.priority}
            min={0}
            max={10000}
            aria-label={`${template.name} 的优先级`}
            onChange={(event) =>
              update.mutate({
                templateId: template.id,
                body: { priority: Number(event.target.value) },
              })
            }
          />
        </td>
        <td className="dim">
          {template.required_labels.length > 0 ? template.required_labels.join(" / ") : "不需要"}
        </td>
        <td className="dim" data-testid="template-source-scope">
          {sourceScopeText(template.source_scope, sources)}
        </td>
        <td><code className="metric-template-table__promql">{template.promql}</code></td>
      </tr>
      {editing ? (
        <tr className="metric-template-editor-row">
          <td colSpan={6}>
            <div className="metric-template-editor">
              <section>
                <h4>来源范围</h4>
                <p className="dim">导入模板默认只用于原来源；需要在其它来源复用时，可以复制后分别限定。</p>
                <select
                  aria-label={`${template.name} 的来源范围`}
                  value={template.source_scope.mode}
                  disabled={pending}
                  onChange={(event) => selectMode(event.target.value as SourceScope["mode"])}
                >
                  <option value="ALL">全部来源</option>
                  <option value="SELECTED">指定来源</option>
                </select>
                {template.source_scope.mode === "SELECTED" ? (
                  <div className="metric-template-editor__sources">
                    {sources.map((source) => (
                      <label key={source.id}>
                        <input
                          type="checkbox"
                          checked={template.source_scope.source_ids.includes(source.id)}
                          disabled={pending}
                          onChange={(event) => toggleSource(source.id, event.target.checked)}
                        /> {source.name}
                      </label>
                    ))}
                  </div>
                ) : null}
              </section>
              {error ? <p className="form-error">{error}</p> : null}
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function MetricTemplateList() {
  const templates = useMetricTemplates();
  const sources = useEventSources();

  if (templates.isLoading || sources.isLoading) return <p className="dim">正在读取指标模板…</p>;
  if (templates.isError || sources.isError) {
    const error = templates.error || sources.error;
    const failure = describeRequestFailure(error, "读取指标模板失败");
    return <p className="dim">{failure.title}：{failure.detail}</p>;
  }

  const rows = templates.data ?? [];
  if (rows.length === 0) return <p className="dim">还没有指标模板。</p>;
  const sourceRows = sources.data ?? [];

  return (
    <div className="section">
      <div className="section__label">指标模板</div>
      <p className="dim">
        主曲线来自告警自己的表达式，不需要配置。这里配的是<strong>辅助曲线</strong>：
        启用后，标签和来源范围都对上的告警旁边会多画这些曲线。
      </p>
      <div className="metric-template-scroll">
        <table className="metric-template-table">
          <thead><tr><th>启用</th><th>名称</th><th>优先级</th><th>需要的标签</th><th>来源范围</th><th>查询语句</th></tr></thead>
          <tbody>
            {rows.map((template) => <MetricTemplateRow key={template.id} template={template} sources={sourceRows} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}
