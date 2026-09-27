/**
 * 来源范围 for a rule — the shared Source Scope vocabulary, rule-side.
 *
 * ALL means every non-archived source; SELECTED never merges incidents across
 * sources, because incidents are source-bound (ADR 0002).
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
    <section className="config-section">
      <div className="config-section__head">
        <div>
          <h3>来源范围</h3>
          <p>默认覆盖所有未归档来源；指定来源后，规则不会跨来源合并 Incident。</p>
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
          {sources.length === 0 && <div className="placeholder-card">还没有来源可选。</div>}
        </div>
      )}
    </section>
  );
}
