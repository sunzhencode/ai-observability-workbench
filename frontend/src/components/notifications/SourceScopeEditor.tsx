/**
 * 来源范围 for a notification policy.
 *
 * Separate from the rules-side editor with the same name: the copy differs
 * because an unknown source means something different when it decides routing
 * than when it decides grouping.
 */
import type { EventSource, SourceScope } from "../../types";

export function SourceScopeEditor({
  sources,
  value,
  onChange,
}: {
  sources: EventSource[];
  value: SourceScope | undefined;
  onChange: (next: SourceScope) => void;
}) {
  const scope = value ?? { mode: "ALL", source_ids: [] };
  return (
    <section className="editor-section">
      <div className="editor-section__head">
        <div>
          <h3>来源范围</h3>
          <p>默认匹配所有未归档来源；新来源不会自动触发存量通知。</p>
        </div>
        <select
          aria-label="来源范围"
          onChange={(event) => onChange({
            mode: event.target.value as SourceScope["mode"],
            source_ids: event.target.value === "ALL" ? [] : scope.source_ids,
          })}
          value={scope.mode}
        >
          <option value="ALL">所有来源</option>
          <option value="SELECTED">指定来源</option>
        </select>
      </div>
      {scope.mode === "SELECTED" && (
        <div className="check-grid">
          {sources.map((source) => (
            <label className="check-card" key={source.id}>
              <input
                checked={scope.source_ids.includes(source.id)}
                onChange={() => {
                  const source_ids = scope.source_ids.includes(source.id)
                    ? scope.source_ids.filter((id) => id !== source.id)
                    : [...scope.source_ids, source.id];
                  onChange({ mode: "SELECTED", source_ids });
                }}
                type="checkbox"
              />
              <span><strong>{source.name}</strong><small>{source.id}</small></span>
            </label>
          ))}
          {sources.length === 0 && <div className="placeholder-card">还没有 EventSource；保存时后端会按当前可见来源校验。</div>}
        </div>
      )}
    </section>
  );
}
