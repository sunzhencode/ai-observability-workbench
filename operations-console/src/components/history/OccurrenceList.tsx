import type { EventSource, IncidentOccurrence } from "../../types";
import type { HistoryConclusion } from "../../appUrl";
import { ageFrom, sevPill } from "../../util";
import { handlingLabel } from "../../handling";

interface Props {
  items: IncidentOccurrence[];
  conclusion: HistoryConclusion | null;
  conclusions: readonly HistoryConclusion[];
  selectedId: number | null;
  selectedSourceIds: string[];
  sources: EventSource[];
  loading: boolean;
  error: boolean;
  hasCursor: boolean;
  nextBeforeId: string | null;
  onConclusionChange: (conclusion: HistoryConclusion | null) => void;
  onSelect: (occurrenceId: number) => void;
  onSourceFilterChange: (sourceIds: string[]) => void;
  onNextPage: (beforeId: string) => void;
  onFirstPage: () => void;
}

/** Duration in the same vocabulary `ageFrom` uses, so the page reads one way. */
function span(startedAt: string, recoveredAt: string): string {
  const started = new Date(startedAt).getTime();
  const recovered = new Date(recoveredAt).getTime();
  if (Number.isNaN(started) || Number.isNaN(recovered)) return "—";
  const secs = Math.max(0, Math.floor((recovered - started) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function OccurrenceList({
  items,
  conclusion,
  conclusions,
  selectedId,
  selectedSourceIds,
  sources,
  loading,
  error,
  hasCursor,
  nextBeforeId,
  onConclusionChange,
  onSelect,
  onSourceFilterChange,
  onNextPage,
  onFirstPage,
}: Props) {
  return (
    <div className="pane pane--list">
      <div className="list-head">
        <div className="list-head__top">
          <span className="list-head__title">发生记录</span>
          <span className="list-head__total">{items.length}</span>
        </div>

        <label className="rule-picker">
          <span>按处理结论收窄</span>
          <select
            aria-label="按处理结论筛选"
            disabled={loading || error}
            onChange={(event) =>
              onConclusionChange(
                event.target.value === ""
                  ? null
                  : (event.target.value as HistoryConclusion),
              )
            }
            value={conclusion ?? ""}
          >
            <option value="">全部结论</option>
            {conclusions.map((item) => (
              <option key={item} value={item}>
                {handlingLabel(item)}
              </option>
            ))}
          </select>
        </label>

        {sources.length > 0 && (
          <div className="source-filter" aria-label="来源筛选">
            <div className="source-filter__head">
              <span>来源</span>
              <button onClick={() => onSourceFilterChange([])} type="button">
                全部
              </button>
            </div>
            {sources.map((source) => (
              <label className="source-filter__item" key={source.id}>
                <input
                  checked={
                    selectedSourceIds.length === 0 ||
                    selectedSourceIds.includes(source.id)
                  }
                  onChange={() => {
                    const current =
                      selectedSourceIds.length === 0
                        ? sources.map((item) => item.id)
                        : selectedSourceIds;
                    const next = current.includes(source.id)
                      ? current.filter((id) => id !== source.id)
                      : [...current, source.id];
                    onSourceFilterChange(
                      next.length === sources.length ? [] : next,
                    );
                  }}
                  type="checkbox"
                />
                <span>{source.name}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      {error && <div className="error">无法加载发生记录，请检查后端。</div>}
      {!error && loading && items.length === 0 && <div className="empty">Loading…</div>}
      {!error && !loading && items.length === 0 && (
        <div className="empty">
          {conclusion !== null || hasCursor ? (
            "当前筛选下没有发生记录。"
          ) : (
            <>
              还没有发生记录。
              <br />
              <small>
                历史从本次升级之后开始累积：一个告警组恢复时才会写下一条，
                升级前发生过的事没有留下可信的恢复时间，不会被补进来。
              </small>
            </>
          )}
        </div>
      )}

      <div className="rows">
        {items.map((item) => (
          <button
            className={`card card--${item.member_max_severity}${
              item.id === selectedId ? " card--active" : ""
            }`}
            key={item.id}
            onClick={() => onSelect(item.id)}
          >
            <div className="card__title">{item.title}</div>
            <div className="card__source">{item.source_name}</div>
            <div className="card__rule card__rule--matched">
              {item.aggregation_rule_name ?? "未命中规则 · 安全隔离"}
              {item.occurrence_no > 1 && ` · 第 ${item.occurrence_no} 次`}
            </div>
            <div className="card__meta">
              <span className={sevPill(item.member_max_severity)}>
                {item.member_max_severity}
              </span>
              <span className="pill pill--count">{item.member_count} 条</span>
              <span
                className={`pill pill--handling pill--handling-${item.handling_conclusion.toLowerCase()}`}
              >
                {handlingLabel(item.handling_conclusion)}
              </span>
              {/* Its own class, not `pill--recovered`: that one is a *state* pill and
                  `.pill` uppercases, which turned `1m` (one minute) into `1M` —
                  readable as one month. A duration is not a state anyway. */}
              <span className="pill pill--duration">
                持续 {span(item.started_at, item.recovered_at)}
              </span>
              <span className="card__age">{ageFrom(item.recovered_at)}</span>
            </div>
          </button>
        ))}
      </div>

      {(hasCursor || nextBeforeId !== null) && (
        <div className="history-pager">
          <button
            className="secondary-btn"
            disabled={!hasCursor}
            onClick={onFirstPage}
            type="button"
          >
            回到最新
          </button>
          <button
            className="secondary-btn"
            disabled={nextBeforeId === null}
            onClick={() => nextBeforeId !== null && onNextPage(nextBeforeId)}
            type="button"
          >
            更早的记录 →
          </button>
        </div>
      )}
    </div>
  );
}
