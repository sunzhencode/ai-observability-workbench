/**
 * 受监测集群 — the persistent Watchdog inventory for one source.
 *
 * Two things about this panel are deliberate and easy to break:
 * it sits *below* 保存并生效 because it is not part of the draft — adding or
 * ignoring a cluster takes effect immediately — and IGNORE removes a cluster
 * from the summary without deleting it (`AGENTS.md`, F20 踩坑).
 */
import type { WatchdogCluster } from "../../types";

export type WatchdogAction = "EXPECT" | "IGNORE" | "RESTORE";

export function WatchdogInventory({
  clusters,
  newCluster,
  busy,
  onNewClusterChange,
  onAdd,
  onChange,
}: {
  clusters: WatchdogCluster[];
  newCluster: string;
  busy: boolean;
  onNewClusterChange: (value: string) => void;
  onAdd: () => void;
  onChange: (cluster: WatchdogCluster, action: WatchdogAction, reason?: string) => void;
}) {
  return (
    <details className="editor-section">
      <summary>
        受监测集群
        <em>{clusters.length > 0 ? `${clusters.length} 个` : "暂无"}</em>
      </summary>
      <div className="watchdog-admin">
        <div className="workbench-url">
          <input
            onChange={(event) => onNewClusterChange(event.target.value)}
            placeholder="手工添加期望集群 identity"
            value={newCluster}
          />
          <button
            className="secondary-btn"
            disabled={busy || !newCluster.trim()}
            onClick={onAdd}
            type="button"
          >
            添加
          </button>
        </div>
        <div className="cluster-table">
          {clusters.map((cluster) => (
            <div className={`cluster-row cluster-row--${cluster.status}`} key={cluster.id}>
              <span className="delivery-symbol" aria-hidden="true">
                ●
              </span>
              <div>
                <strong>{cluster.cluster}</strong>
                <small>
                  {cluster.inventory_state} · {cluster.health_state ?? "UNKNOWN"} · 最后看到{" "}
                  {cluster.last_seen_at ? new Date(cluster.last_seen_at).toLocaleString() : "—"}
                </small>
                {cluster.ignored_reason && <small>忽略原因：{cluster.ignored_reason}</small>}
              </div>
              <div className="inline-actions">
                {cluster.inventory_state === "IGNORED" ? (
                  <button
                    className="secondary-btn"
                    onClick={() => onChange(cluster, "RESTORE")}
                    type="button"
                  >
                    恢复
                  </button>
                ) : (
                  <button
                    className="secondary-btn"
                    onClick={() => {
                      const reason = window.prompt("忽略原因");
                      if (reason !== null) onChange(cluster, "IGNORE", reason);
                    }}
                    type="button"
                  >
                    忽略
                  </button>
                )}
                {cluster.inventory_state === "DISCOVERED" && (
                  <button
                    className="secondary-btn"
                    onClick={() => onChange(cluster, "EXPECT")}
                    type="button"
                  >
                    设为期望
                  </button>
                )}
              </div>
            </div>
          ))}
          {clusters.length === 0 && (
            <div className="placeholder-card">该来源暂无 Watchdog inventory。</div>
          )}
        </div>
      </div>
    </details>
  );
}
