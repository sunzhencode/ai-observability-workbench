/**
 * Pick a dashboard, review every candidate it produced, tick the ones to keep.
 *
 * A temporary flow, so its state stays here and out of the URL — the same call
 * the channel test dialog already made. Nothing here is worth restoring on a
 * page reload, and a half-finished import in a shareable link would be worse
 * than useless.
 *
 * The layout answers the two questions a reader has about each candidate: what
 * exactly will be stored, and how much do we actually know about it. So the
 * detail pane shows the original query beside the resolved one, names every
 * macro that was substituted, and reports what the bounded live probe saw.
 */
import { useMemo, useState } from "react";
import {
  useConfirmGrafanaImport,
  useGrafanaDashboards,
  usePreviewGrafanaImport,
} from "../../queries/grafana";
import { requestFailureText } from "../../requestError";
import {
  buildImportItems,
  canSubmitCandidate,
  candidateKey,
  initialDraft,
  isSelectable,
  probeValueLabel,
  selectedCount,
  statusLabel,
  type CandidateDraft,
  type ImportDraft,
  type VariableDecision,
} from "../../grafanaImportDraft";
import type { EventSource, GrafanaImportPreview, ImportCandidate } from "../../types";

interface Props {
  source: EventSource;
  onClose: () => void;
}

export function GrafanaImportDrawer({ source, onClose }: Props) {
  const dashboards = useGrafanaDashboards(source.id, true);
  const preview = usePreviewGrafanaImport();
  const confirm = useConfirmGrafanaImport();

  const [loaded, setLoaded] = useState<GrafanaImportPreview | null>(null);
  const [draft, setDraft] = useState<ImportDraft>({});
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const candidates = loaded?.candidates ?? [];
  const selected = useMemo(
    () => candidates.find((item) => candidateKey(item) === selectedKey) ?? null,
    [candidates, selectedKey],
  );
  const chosen = selectedCount(candidates, draft);

  const openDashboard = async (uid: string) => {
    setMessage(null);
    try {
      const result = await preview.mutateAsync({ sourceId: source.id, dashboardUid: uid });
      setLoaded(result);
      setDraft(initialDraft(result.candidates));
      setSelectedKey(
        result.candidates.length > 0 ? candidateKey(result.candidates[0]) : null,
      );
    } catch (error) {
      setMessage(requestFailureText(error, "导入操作失败"));
    }
  };

  const patch = (key: string, changes: Partial<CandidateDraft>) => {
    setDraft((current) => ({ ...current, [key]: { ...current[key], ...changes } }));
  };

  const decide = (key: string, variable: string, decision: VariableDecision) => {
    setDraft((current) => ({
      ...current,
      [key]: {
        ...current[key],
        decisions: { ...current[key].decisions, [variable]: decision },
      },
    }));
  };

  const onConfirm = async () => {
    if (loaded === null) return;
    setMessage(null);
    const items = buildImportItems(
      loaded.dashboard_uid,
      loaded.dashboard_title,
      candidates,
      draft,
    );
    try {
      const result = await confirm.mutateAsync({ sourceId: source.id, items });
      const created = result.created_template_ids.length;
      const updated = result.updated_template_ids.length;
      // Re-preview first, so what is on screen matches what is stored:
      // everything just written now aligns as "unchanged" and drops out of the
      // list. The message goes *after* it, because `openDashboard` clears the
      // message — announcing the result and then wiping it half a second later
      // left the user with no confirmation that anything had happened.
      await openDashboard(loaded.dashboard_uid);
      setMessage(`已导入 ${created} 条，更新 ${updated} 条。`);
    } catch (error) {
      setMessage(requestFailureText(error, "导入操作失败"));
    }
  };

  return (
    <div className="drawer" data-testid="grafana-import-drawer">
      <header className="editor-head">
        <div>
          <span className="eyebrow">{source.name}</span>
          <h2>从 Grafana 导入指标模板</h2>
        </div>
        <button className="secondary-btn" onClick={onClose} type="button">
          关闭
        </button>
      </header>

      <section className="editor-section">
        <h3>选择 dashboard</h3>
        {dashboards.isPending && <p className="hint">正在读取 dashboard 列表…</p>}
        {dashboards.isError && (
          <p className="hint">{requestFailureText(dashboards.error, "dashboard 列表加载失败")}</p>
        )}
        <div className="inline-actions" data-testid="grafana-dashboard-list">
          {(dashboards.data ?? []).map((item) => (
            <button
              className={
                loaded?.dashboard_uid === item.uid ? "primary-btn" : "secondary-btn"
              }
              disabled={preview.isPending}
              key={item.uid}
              onClick={() => openDashboard(item.uid)}
              type="button"
            >
              {item.title}
            </button>
          ))}
        </div>
        {preview.isPending && <p className="hint">正在解析并逐条实查…</p>}
      </section>

      {loaded !== null && (
        <>
          {!loaded.probed && (
            <p className="hint" data-testid="grafana-unprobed-note">
              这个来源没有配置历史地址（Thanos），无法验证这些查询能不能跑，
              下面全部标为「未验证」。
            </p>
          )}

          <section className="editor-section import-layout">
            <ul className="import-candidates" data-testid="grafana-candidate-list">
              {candidates.map((candidate) => {
                const key = candidateKey(candidate);
                const item = draft[key];
                const selectable = isSelectable(candidate);
                return (
                  <li
                    className={selectedKey === key ? "member member--active" : "member"}
                    key={key}
                  >
                    <label className="switch-row">
                      <input
                        checked={item?.checked ?? false}
                        disabled={!selectable || !canSubmitCandidate(candidate, item)}
                        onChange={(event) =>
                          patch(key, { checked: event.target.checked })
                        }
                        type="checkbox"
                      />
                      <button
                        className="link-btn"
                        onClick={() => setSelectedKey(key)}
                        type="button"
                      >
                        {candidate.suggested_name || candidate.panel_title}
                      </button>
                    </label>
                    <span
                      className={`pill ${badgeClass(candidate)}`}
                      data-testid="candidate-badge"
                    >
                      {statusLabel(candidate)}
                    </span>
                  </li>
                );
              })}
              {candidates.length === 0 && (
                <li className="member">这个 dashboard 没有可导入的 panel。</li>
              )}
            </ul>

            {selected !== null && draft[candidateKey(selected)] !== undefined && (
              <CandidateDetail
                candidate={selected}
                draft={draft[candidateKey(selected)]}
                onDecide={(variable, decision) =>
                  decide(candidateKey(selected), variable, decision)
                }
                onPatch={(changes) => patch(candidateKey(selected), changes)}
              />
            )}
          </section>

          <section className="editor-section">
            <p data-testid="grafana-curve-budget">
              当前已启用 {loaded.enabled_template_count} 条模板；
              一条告警最多画 {loaded.max_auxiliary_curves} 条，
              超出的按 priority 顺序被略过。
            </p>
            <div className="inline-actions">
              <button
                className="primary-btn"
                data-testid="grafana-import-confirm"
                disabled={chosen === 0 || confirm.isPending}
                onClick={onConfirm}
                type="button"
              >
                导入选中的 {chosen} 条
              </button>
            </div>
          </section>
        </>
      )}

      {message !== null && (
        <p className="hint" data-testid="grafana-import-message" role="status">
          {message}
        </p>
      )}
    </div>
  );
}

function CandidateDetail({
  candidate,
  draft,
  onDecide,
  onPatch,
}: {
  candidate: ImportCandidate;
  draft: CandidateDraft;
  onDecide: (variable: string, decision: VariableDecision) => void;
  onPatch: (changes: Partial<CandidateDraft>) => void;
}) {
  return (
    <div className="import-detail" data-testid="grafana-candidate-detail">
      <h3>{candidate.panel_title}</h3>

      {candidate.status === "UNSUPPORTED" && (
        <p className="hint" data-testid="candidate-reason">
          {candidate.reason}
        </p>
      )}
      {candidate.probe_note !== "" && (
        <p className="hint" data-testid="candidate-probe-note">
          {candidate.probe_note}
        </p>
      )}

      <label className="form-grid__wide">
        <span>模板名称</span>
        <input
          onChange={(event) => onPatch({ name: event.target.value })}
          value={draft.name}
        />
      </label>

      <h4>Grafana 原文</h4>
      <pre className="code-block">{candidate.raw_promql}</pre>
      {candidate.resolved_promql !== candidate.raw_promql && (
        <>
          <h4>替换后</h4>
          <pre className="code-block" data-testid="candidate-resolved">
            {candidate.resolved_promql}
          </pre>
        </>
      )}
      {candidate.substitutions.length > 0 && (
        <ul data-testid="candidate-substitutions">
          {candidate.substitutions.map((item) => (
            <li key={item.macro}>
              已把 <code>{item.macro}</code> 替换为 <code>{item.replacement}</code>
            </li>
          ))}
        </ul>
      )}

      {(candidate.diff_kind === "UPSTREAM_CHANGED" ||
        candidate.diff_kind === "CONFLICT") && (
        <div data-testid="candidate-diff">
          <h4>{candidate.diff_kind === "CONFLICT" ? "冲突：三份都在这里" : "上游已变"}</h4>
          <p className="hint">导入时是：</p>
          <pre className="code-block">{candidate.imported_promql}</pre>
          {candidate.diff_kind === "CONFLICT" && (
            <>
              <p className="hint">你改成了：</p>
              <pre className="code-block">{candidate.current_promql}</pre>
            </>
          )}
          <p className="hint">Grafana 现在是上面的「替换后」。由你决定要不要接受。</p>
        </div>
      )}

      {candidate.pending_variables.length > 0 && (
        <div data-testid="candidate-variables">
          <h4>这些变量要你决定</h4>
          {candidate.pending_variables.map((variable) => {
            const decision = draft.decisions[variable.name];
            return (
              <div className="form-grid" key={variable.name}>
                <label>
                  <span>${variable.name}</span>
                  <select
                    onChange={(event) => {
                      const kind = event.target.value;
                      if (kind === "BIND_LABEL")
                        onDecide(variable.name, {
                          kind: "BIND_LABEL",
                          label: variable.name,
                        });
                      else if (kind === "PIN_VALUE")
                        onDecide(variable.name, {
                          kind: "PIN_VALUE",
                          value: variable.sample_value,
                        });
                      else if (kind === "SKIP")
                        onDecide(variable.name, { kind: "SKIP" });
                      else onDecide(variable.name, { kind: "UNDECIDED" });
                    }}
                    value={decision?.kind ?? "UNDECIDED"}
                  >
                    <option value="UNDECIDED">请选择</option>
                    <option value="BIND_LABEL">绑到告警标签</option>
                    <option value="PIN_VALUE">填死一个值</option>
                    <option value="SKIP">跳过这一条</option>
                  </select>
                  <span className="hint">
                    建议：
                    {variable.suggestion === "BIND_LABEL"
                      ? `绑到告警标签 ${variable.name}`
                      : `填死为 ${variable.sample_value || "当前值"}`}
                  </span>
                </label>
                {decision?.kind === "BIND_LABEL" && (
                  <label>
                    <span>标签名</span>
                    <input
                      onChange={(event) =>
                        onDecide(variable.name, {
                          kind: "BIND_LABEL",
                          label: event.target.value,
                        })
                      }
                      value={decision.label}
                    />
                  </label>
                )}
                {decision?.kind === "PIN_VALUE" && (
                  <label>
                    <span>固定值</span>
                    <input
                      onChange={(event) =>
                        onDecide(variable.name, {
                          kind: "PIN_VALUE",
                          value: event.target.value,
                        })
                      }
                      value={decision.value}
                    />
                  </label>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div data-testid="candidate-probe-result">
        <h4>实查结果</h4>
        <p className="hint" data-testid="candidate-probe-value">
          {probeValueLabel(candidate)}
          {candidate.probe_note ? ` · ${candidate.probe_note}` : ""}
        </p>
      </div>

      <label className="switch-row">
        <input
          checked={draft.enabled}
          onChange={(event) => onPatch({ enabled: event.target.checked })}
          type="checkbox"
        />
        <span>导入后立即启用（默认关闭，先看一眼再开）</span>
      </label>
    </div>
  );
}

/**
 * Reuses the existing pill palette rather than inventing colours. "Unverified"
 * and "cannot use" share the muted tone deliberately — both mean "not usable as
 * it stands", and the label beside the colour is what says which, since the two
 * lead to completely different next steps.
 */
function badgeClass(candidate: ImportCandidate): string {
  if (candidate.diff_kind === "CONFLICT") return "pill--critical";
  if (candidate.diff_kind === "UPSTREAM_CHANGED") return "pill--info";
  switch (candidate.status) {
    case "READY":
      return "pill--info";
    case "NEEDS_DECISION":
      return "pill--warning";
    default:
      return "pill--unknown";
  }
}
