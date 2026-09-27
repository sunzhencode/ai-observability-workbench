/**
 * 系统设置 — the container. The five panels around it are in
 * `components/settings/`.
 *
 * Which source is open, and whether archived ones are listed, are in the URL:
 * that is what `/settings?source=<id>` from an incident links to, and what a
 * reload restores. The editing draft stays local — it is unsaved text, not a
 * place worth linking to.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { SourceAudit } from "../components/settings/SourceAudit";
import { SourceForm } from "../components/settings/SourceForm";
import { GrafanaPanel } from "../components/settings/GrafanaPanel";
import { GrafanaImportDrawer } from "../components/settings/GrafanaImportDrawer";
import { SourceList } from "../components/settings/SourceList";
import { ModelChannelsPanel } from "../components/settings/ModelChannelsPanel";
import {
  WatchdogInventory,
  type WatchdogAction,
} from "../components/settings/WatchdogInventory";
import {
  SETTINGS_TABS,
  parseSettingsTab,
  settingsArchivedFromSearch,
  settingsPath,
  settingsSourceFromSearch,
} from "../appUrl";
import { explainSaveError, sourceModeSummary } from "../settingsStatus";
import { resolveSelectedSource, visibleSources } from "../selection";
import { canSave, draftFromSource, emptySourceDraft, withAssignedPositions } from "../sourceDraft";
import { useEventSources } from "../queries/catalog";
import { useHealth } from "../queries/health";
import {
  useAddWatchdogCluster,
  useEventSourceAudit,
  useEventSourceLifecycle,
  useSaveEventSource,
  useTestEventSource,
  useUpdateWatchdogCluster,
  useWatchdogClusters,
  type SourceLifecycleAction,
} from "../queries/eventSources";
import type { EventSourceCandidate, EventSourceTestResult, WatchdogCluster } from "../types";

/** `?source=new` is the add form; any other value names an existing source. */
const NEW_SOURCE = "new";

const SETTINGS_TAB_LABELS: Record<(typeof SETTINGS_TABS)[number], string> = {
  sources: "数据源",
  model: "模型服务",
};

export function SettingsPage() {
  const navigate = useNavigate();
  const params = useParams();
  const activeTab = parseSettingsTab(params.tab);
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.toString();

  const urlSource = settingsSourceFromSearch(search);
  const showArchived = settingsArchivedFromSearch(search);
  const creating = urlSource === NEW_SOURCE;

  const [draft, setDraft] = useState<EventSourceCandidate>(emptySourceDraft());
  const [testResult, setTestResult] = useState<EventSourceTestResult | null>(null);
  const [newCluster, setNewCluster] = useState("");
  // The import drawer is a temporary flow, so it stays out of the URL — the
  // same call the channel test dialog already made.
  const [importOpen, setImportOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const health = useHealth();
  // Always fetched with archived included: the checkbox only changes what this
  // page lists, and refetching the registry to toggle a filter would make the
  // list flicker for a decision the client can make.
  const sourcesQuery = useEventSources(true);
  const sources = useMemo(() => sourcesQuery.data ?? [], [sourcesQuery.data]);
  const visible = useMemo(() => visibleSources(sources, showArchived), [sources, showArchived]);

  const selectedId = creating ? null : resolveSelectedSource(urlSource, visible);
  const selected = useMemo(
    () => (creating ? null : sources.find((item) => item.id === selectedId) ?? null),
    [creating, sources, selectedId],
  );

  const auditQuery = useEventSourceAudit(selected?.id ?? null);
  const clustersQuery = useWatchdogClusters(selected?.id ?? null);

  const saveSource = useSaveEventSource();
  const lifecycle = useEventSourceLifecycle();
  const testSource = useTestEventSource();
  const addCluster = useAddWatchdogCluster();
  const updateCluster = useUpdateWatchdogCluster();

  const busy =
    saveSource.isPending ||
    lifecycle.isPending ||
    testSource.isPending ||
    addCluster.isPending ||
    updateCluster.isPending;

  // Reset the form when the stored configuration actually moves on. Keyed on
  // identity and version rather than the object: every successful action
  // refetches the list and hands back new objects, and keying on the object
  // wiped both the connection-test result the user had just asked for and any
  // unsaved edits. A connection test does not bump `version`.
  useEffect(() => {
    if (creating) {
      setDraft(emptySourceDraft());
      setTestResult(null);
      return;
    }
    setImportOpen(false);
    if (!selected) return;
    setDraft(draftFromSource(selected));
    setTestResult(null);
  }, [creating, selected?.id, selected?.version]);

  const setParam = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(search);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null) next.delete(key);
        else next.set(key, value);
      }
      setSearchParams(next, { replace: true });
    },
    [search, setSearchParams],
  );

  const selectSource = useCallback(
    (sourceId: string) => {
      setMessage("");
      setError("");
      setParam({ source: sourceId });
    },
    [setParam],
  );

  const startNew = useCallback(() => {
    setMessage("");
    setError("");
    setParam({ source: NEW_SOURCE });
  }, [setParam]);

  /** Any draft edit invalidates a result measured against the saved address. */
  const editDraft = useCallback((next: EventSourceCandidate) => {
    setDraft(next);
    setTestResult(null);
  }, []);

  const run = async (action: () => Promise<unknown>, success: string) => {
    if (busy) return;
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(success);
    } catch (cause) {
      setError(cause instanceof Error ? explainSaveError(cause.message) : "操作失败。");
    }
  };

  const save = () =>
    run(async () => {
      const payload = withAssignedPositions(draft);
      const saved = await saveSource.mutateAsync({
        sourceId: selected?.id ?? null,
        draft: payload,
        version: selected?.version ?? null,
      });
      setParam({ source: saved.id });
    }, selected ? "已保存并生效，下一轮轮询开始使用新配置。" : "来源已添加并开始轮询。");

  const test = async () => {
    if (!selected || busy) return;
    setError("");
    setMessage("");
    try {
      // A failed connection test is a 200 with ok=false, so it must not be
      // reported through the success path -- CAP-01.7 requires the failure to
      // be visible.
      const result = await testSource.mutateAsync(selected.id);
      setTestResult(result);
      if (result.ok) setMessage("已对保存的地址完成一次只读 GET 连接测试。");
      else setError("连接测试失败，详情见下方结果。");
    } catch (cause) {
      setError(cause instanceof Error ? explainSaveError(cause.message) : "连接测试失败。");
    }
  };

  const changeLifecycle = (action: SourceLifecycleAction, confirmText: string, success: string) => {
    if (!selected) return;
    if (!window.confirm(confirmText)) return;
    void run(
      () => lifecycle.mutateAsync({ sourceId: selected.id, version: selected.version, action }),
      success,
    );
  };

  const changeCluster = (cluster: WatchdogCluster, action: WatchdogAction, reason?: string) => {
    if (!selected) return;
    void run(
      () =>
        updateCluster.mutateAsync({
          sourceId: selected.id,
          clusterId: cluster.id,
          action,
          reason,
        }),
      "Watchdog 清单已更新。",
    );
  };

  const addExpectedCluster = () => {
    if (!selected || !newCluster.trim()) return;
    void run(async () => {
      await addCluster.mutateAsync({ sourceId: selected.id, cluster: newCluster.trim() });
      setNewCluster("");
    }, "已添加期望 Watchdog 集群。");
  };

  const managedCount = useMemo(
    () => sources.filter((item) => item.lifecycle_state !== "ARCHIVED").length,
    [sources],
  );
  const mode = useMemo(
    () => sourceModeSummary(health.data ?? null, managedCount),
    [health.data, managedCount],
  );

  const editable = selected === null || selected.lifecycle_state !== "ARCHIVED";
  const listError = sourcesQuery.isError ? "数据源加载失败。" : "";
  const detailError = auditQuery.isError || clustersQuery.isError
    ? "变更历史或 Watchdog 清单加载失败。"
    : "";

  // Same `.domain-tabs` convention as 聚合规则 and 通知. 模型服务 is a tab rather
  // than a section under the source list: it is a *different kind of thing* —
  // a paid outbound target with its own DRAFT → tested → active gate — and
  // putting it below the sources made it read as an accessory to one.
  const tabs = (
    <div className="domain-tabs" role="tablist" aria-label="系统设置">
      {SETTINGS_TABS.map((name) => (
        <button
          key={name}
          type="button"
          role="tab"
          aria-selected={name === activeTab}
          className={name === activeTab ? "domain-tab--active" : ""}
          // push, not replace: switching halves of the page is navigation, and
          // the back button should undo it (F23).
          onClick={() => navigate(settingsPath(name))}
        >
          {SETTINGS_TAB_LABELS[name]}
        </button>
      ))}
    </div>
  );

  if (activeTab === "model") {
    return (
      <div className="settings-shell">
        {tabs}
        <main className="settings-page management-page">
          <ModelChannelsPanel />
        </main>
      </div>
    );
  }

  return (
    <div className="settings-shell">
      {tabs}
      <main className="settings-page management-page">
      <section className="management-hero">
        <div>
          <span className="eyebrow">Data sources</span>
          <h1>数据源</h1>
          <p>只读接入 Alertmanager 与历史数据；凭证只以密文存在本地，保存即生效。</p>
        </div>
        <button
          className="secondary-btn"
          onClick={() => void sourcesQuery.refetch()}
          type="button"
        >
          刷新
        </button>
      </section>

      <div className={`source-mode source-mode--${mode.tone}`} role="status">
        <strong>{mode.title}</strong>
        {mode.detail && <span>{mode.detail}</span>}
      </div>

      <div className="resource-split settings-split">
        <SourceList
          creating={creating}
          onSelect={selectSource}
          onShowArchivedChange={(value) => setParam({ archived: value ? "1" : null })}
          onStartNew={startNew}
          selectedId={selectedId}
          showArchived={showArchived}
          sources={visible}
        />

        <section className="resource-editor">
          <SourceForm
            busy={busy}
            draft={draft}
            editable={editable}
            onArchive={() =>
              changeLifecycle(
                "archive",
                `归档来源 ${selected?.name}？归档后不会出现在默认运行视图。`,
                "来源已归档。",
              )
            }
            onDisable={() =>
              changeLifecycle(
                "disable",
                `停用来源 ${selected?.name}？现有活动 Incident 会保持 STALE，不会发送恢复。`,
                "来源已停用。",
              )
            }
            onEnable={() => changeLifecycle("enable", `启用来源 ${selected?.name}？`, "来源已启用。")}
            onSave={() => void save()}
            onTest={() => void test()}
            saveDisabled={busy || !canSave(draft)}
            selected={selected}
            setDraft={editDraft}
            testResult={testResult}
          />

          {selected && (
            <GrafanaPanel
              editable={editable}
              onOpenImport={() => setImportOpen(true)}
              source={selected}
            />
          )}

          {selected && importOpen && (
            <GrafanaImportDrawer onClose={() => setImportOpen(false)} source={selected} />
          )}

          {selected && (
            <WatchdogInventory
              busy={busy}
              clusters={clustersQuery.data ?? []}
              newCluster={newCluster}
              onAdd={addExpectedCluster}
              onChange={changeCluster}
              onNewClusterChange={setNewCluster}
            />
          )}

          {selected && <SourceAudit audit={auditQuery.data ?? []} />}

          {message && (
            <div className="toast-line toast-line--ok" role="status">
              {message}
            </div>
          )}
          {(error || listError || detailError) && (
            <div className="toast-line toast-line--error" role="alert">
              {error || listError || detailError}
            </div>
          )}
        </section>
      </div>
      </main>
    </div>
  );
}
