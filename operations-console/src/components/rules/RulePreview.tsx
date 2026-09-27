/**
 * 规则预览 and the save bar.
 *
 * The preview considers existing rule priority but writes nothing, and saving
 * is gated on having one: publishing a rule regroups every incident, and the
 * counts here are the only warning before that happens.
 */
import type { AggregationRulePreview } from "../../types";

export function RulePreview({
  preview,
  previewing,
  saving,
  saveState,
  error,
  isNew,
  onRunPreview,
  onSave,
}: {
  preview: AggregationRulePreview | null;
  previewing: boolean;
  saving: boolean;
  saveState: "idle" | "saved" | "error";
  error: string;
  isNew: boolean;
  onRunPreview: () => void;
  onSave: () => void;
}) {
  const groupingExplosion =
    preview !== null &&
    preview.selected_alert_count > 1 &&
    preview.proposed_group_count / preview.selected_alert_count >= 0.8;

  return (
    <>
        <section className="config-section">
          <div className="config-section__head">
            <div>
              <h3>规则预览</h3>
              <p>预览会考虑现有规则优先级，但不会写数据库。</p>
            </div>
            <button className="secondary-btn" disabled={previewing} onClick={onRunPreview} type="button">
              {previewing ? "计算中…" : "预览规则"}
            </button>
          </div>
          {!preview ? (
            <div className="placeholder-card">修改规则后先预览，再保存。</div>
          ) : (
            <div className="preview-card">
              <div className="preview-card__counts">
                matcher 命中 <strong>{preview.matcher_alert_count}</strong>
                <span>·</span>
                优先级后实际分配 <strong>{preview.selected_alert_count}</strong>
                <span>·</span>
                生成 <strong>{preview.proposed_group_count}</strong> 组
              </div>
              {groupingExplosion && (
                <div className="preview-warning">组数接近告警数，当前聚合标签可能没有产生有效收敛。</div>
              )}
              {preview.groups.some((group) => group.missing_labels.length > 0) && (
                <div className="preview-warning">
                  部分告警缺少聚合标签，将继续按 fingerprint 隔离。
                </div>
              )}
              {preview.by_source.length > 0 && (
                <div className="preview-source-grid">
                  {preview.by_source.map((source) => (
                    <div className="preview-source" key={source.source_id}>
                      <strong>{source.source_name ?? source.source_id}</strong>
                      <span>命中 {source.matcher_alert_count}</span>
                      <span>分配 {source.selected_alert_count}</span>
                      <span>组 {source.proposed_group_count}</span>
                    </div>
                  ))}
                </div>
              )}
              <details>
                <summary>查看 {preview.groups.length} 个预期分组</summary>
                {preview.groups.map((group) => (
                  <div className="preview-group" key={group.group_key}>
                    <strong>{group.title}</strong>
                    <code>{group.fingerprints.join(", ")}</code>
                  </div>
                ))}
              </details>
            </div>
          )}
        </section>

        {error && <div className="config-error">{error}</div>}
        <footer className="config-savebar">
          <div>
            {!preview && saveState === "idle" && "需要先预览当前草稿。"}
            {saveState === "saved" && "规则已保存，现存告警已按全部启用规则重新分组。"}
            {saveState === "error" && "保存失败，规则和告警分组均未部分写入。"}
          </div>
          <button className="primary-btn" disabled={!preview || saving} onClick={onSave} type="button">
            {saving ? "保存中…" : isNew ? "创建规则" : "保存规则"}
          </button>
        </footer>
    </>
  );
}
