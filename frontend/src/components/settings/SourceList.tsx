/**
 * The registry, on the left.
 *
 * Selecting a row is a link, not local state: `?source=<id>` is what the alerts
 * page links into and what a reload restores.
 */
import { sourceStatus, toneClassName } from "../../settingsStatus";
import type { EventSource } from "../../types";

export function SourceList({
  sources,
  selectedId,
  creating,
  showArchived,
  onSelect,
  onStartNew,
  onShowArchivedChange,
}: {
  sources: EventSource[];
  selectedId: string | null;
  creating: boolean;
  showArchived: boolean;
  onSelect: (sourceId: string) => void;
  onStartNew: () => void;
  onShowArchivedChange: (value: boolean) => void;
}) {
  return (
    <aside className="resource-list">
      <div className="resource-list__head">
        <div>
          <span className="eyebrow">Registry</span>
          <h2>数据源</h2>
        </div>
        <button className="secondary-btn" onClick={onStartNew} type="button">
          + 添加数据源
        </button>
      </div>
      <label className="resource-list__filter">
        <input
          checked={showArchived}
          onChange={(event) => onShowArchivedChange(event.target.checked)}
          type="checkbox"
        />
        <span>显示已归档</span>
      </label>
      {sources.map((source) => {
        const status = sourceStatus(source);
        return (
          <button
            className={`resource-row${
              !creating && source.id === selectedId ? " resource-row--active" : ""
            }`}
            key={source.id}
            onClick={() => onSelect(source.id)}
            type="button"
          >
            <span>
              <strong>{source.name}</strong>
              <small>{source.config?.endpoints[0]?.url ?? source.id}</small>
            </span>
            <i className={`resource-state ${toneClassName(status.tone)}`}>
              {status.label}
              {status.nextStep && <em>{status.nextStep}</em>}
            </i>
          </button>
        );
      })}
      {sources.length === 0 && (
        <div className="empty">
          还没有数据源。点「添加数据源」填一个 Alertmanager 地址就能开始；在此之前系统不会
          轮询任何地址。
        </div>
      )}
    </aside>
  );
}
