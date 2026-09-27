/**
 * 策略 — which incidents go to which channel.
 *
 * Two safety properties here are not cosmetic:
 *  - saving has no side effect; activating requires recomputing the impact and
 *    confirming, because activation can queue messages for incidents that are
 *    already firing;
 *  - disabling only affects later occurrences — incidents whose route is
 *    already locked finish where they were sent (CAP-07).
 */
import { useMemo, useState } from "react";
import { useNotificationCommands } from "../../queries/notifications";
import { describeRequestFailure } from "../../requestError";
import type {
  EventSource,
  MatcherOperator,
  NotificationChannel,
  NotificationMatcher,
  NotificationPolicy,
  NotificationPolicyDraft,
  NotificationPolicyPreview,
  PreparedPolicyActivation,
} from "../../types";
import {
  channelStateLabel,
  draftFromPolicy,
  emptyPolicy,
  latestPolicies,
  OPERATORS,
  policyStateLabel,
  STABLE_FIELDS,
} from "./shared";
import { SourceScopeEditor } from "./SourceScopeEditor";

const REPEAT_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: "关闭" },
  { value: 1800, label: "30 分钟" },
  { value: 3600, label: "1 小时" },
  { value: 14400, label: "4 小时" },
  { value: 43200, label: "12 小时" },
  { value: 86400, label: "24 小时" },
];

export function PoliciesPanel({
  channels,
  policies,
  sources,
  selectedLogicalId,
  onSelect,
  refresh,
}: {
  channels: NotificationChannel[];
  policies: NotificationPolicy[];
  sources: EventSource[];
  selectedLogicalId: string | null;
  onSelect: (logicalId: string | null) => void;
  refresh: () => Promise<unknown>;
}) {
  const commands = useNotificationCommands();
  const latest = useMemo(() => latestPolicies(policies), [policies]);
  const selected = latest.find((item) => item.logical_id === selectedLogicalId) ?? null;

  const [draft, setDraft] = useState<NotificationPolicyDraft>(() => emptyPolicy(channels[0]?.id));
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [preview, setPreview] = useState<NotificationPolicyPreview | null>(null);
  const [prepared, setPrepared] = useState<PreparedPolicyActivation | null>(null);
  const [notifyExisting, setNotifyExisting] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  // Load the URL's policy into the draft once per identity+version, during
  // render rather than in an effect: the selection comes from the address bar,
  // so it can change before this component ever gets to run an effect.
  const identity = selected ? `${selected.logical_id}@${selected.version}` : "new";
  if (loadedFor !== identity) {
    setLoadedFor(identity);
    setDraft(selected ? draftFromPolicy(selected) : emptyPolicy(channels[0]?.id));
    setPreview(null);
    setPrepared(null);
    setNotifyExisting(false);
  }

  const invalidate = () => {
    setPreview(null);
    setPrepared(null);
    setMessage("");
    setError("");
  };

  const updateDraft = (patch: Partial<NotificationPolicyDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
    invalidate();
  };

  const updateMatcher = (index: number, patch: Partial<NotificationMatcher>) => {
    updateDraft({
      matchers: draft.matchers.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    });
  };

  const act = async (key: string, action: () => Promise<unknown>, success: string) => {
    if (busy) return;
    setBusy(key);
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(success);
      await refresh();
    } catch (cause) {
      const failure = describeRequestFailure(cause, "通知策略变更未完成；请刷新状态后重试。");
      setError(failure.detail ?? failure.title);
    } finally {
      setBusy("");
    }
  };

  const save = () =>
    act(
      "save",
      async () => {
        const saved = selected
          ? await commands.updateNotificationPolicy(selected.logical_id, draft, selected.version)
          : await commands.createNotificationPolicy(draft);
        onSelect(saved.logical_id);
      },
      "策略更改已保存；没有启用，也没有发送存量事件。",
    );

  const prepare = async () => {
    if (!selected || selected.state !== "DRAFT") return;
    setBusy("prepare");
    setError("");
    try {
      setPrepared(
        await commands.prepareNotificationPolicyActivation(
          selected.id,
          selected.version,
          notifyExisting,
        ),
      );
    } catch (cause) {
      const failure = describeRequestFailure(cause, "启用影响计算失败。");
      setError(failure.detail ?? failure.title);
    } finally {
      setBusy("");
    }
  };

  const confirmActivation = () => {
    if (!selected || !prepared) return;
    const consequence = notifyExisting
      ? `启用后会为当前 ${prepared.eligible_incident_count} 个符合条件的 firing 事件排队；将按通道限流发送。`
      : "只通知启用后的新变化，不通知当前存量事件。";
    if (!window.confirm(`确认启用策略 ${selected.name}？\n${consequence}`)) return;
    void act(
      "activate",
      () =>
        commands.activateNotificationPolicy(
          selected.id,
          selected.version,
          notifyExisting,
          prepared.confirm_token,
        ),
      "通知策略已启用。 ",
    );
  };

  const disablePolicy = () => {
    if (!selected) return;
    const warning =
      `禁用策略 ${selected.name}？只影响后续 occurrence；已锁定的 Incident 路由继续按原去向完成。`;
    if (!window.confirm(warning)) return;
    void act("disable", () => commands.disableNotificationPolicy(selected.id, selected.version), "策略已禁用。");
  };

  return (
    <div className="resource-split">
      <aside className="resource-list">
        <div className="resource-list__head">
          <div>
            <span className="eyebrow">Routing</span>
            <h2>策略</h2>
          </div>
          <button className="secondary-btn" onClick={() => onSelect(null)} type="button">
            + 新建
          </button>
        </div>
        {latest.map((item) => (
          <button
            className={`resource-row${
              selectedLogicalId === item.logical_id ? " resource-row--active" : ""
            }`}
            key={item.logical_id}
            onClick={() => onSelect(item.logical_id)}
            type="button"
          >
            <span>
              <strong>
                {item.priority} · {item.name}
              </strong>
              <small>
                {policyStateLabel(item.state)} · {item.channel_ids.length} 通道 · repeat{" "}
                {item.repeat_interval_seconds === 0
                  ? "off"
                  : `${Math.round(item.repeat_interval_seconds / 60)}m`}
              </small>
            </span>
            <i className={`resource-state resource-state--${item.state.toLowerCase()}`}>
              {policyStateLabel(item.state)}
            </i>
          </button>
        ))}
        {latest.length === 0 && (
          <div className="empty">没有策略。默认不会发送任何 Incident 通知。</div>
        )}
      </aside>

      <section className="resource-editor">
        <header className="editor-head">
          <div>
            <span className="eyebrow">{selected ? policyStateLabel(selected.state) : "添加策略"}</span>
            <h2>{selected?.name ?? "添加通知策略"}</h2>
          </div>
          {selected?.state === "ACTIVE" && (
            <button className="danger-btn" onClick={disablePolicy} type="button">
              禁用策略
            </button>
          )}
        </header>

        <div className="boundary-note">
          策略只匹配稳定的 Incident 字段；数字越小越先执行，第一条命中即锁定本次 occurrence 的通道。
        </div>

        <section className="editor-section">
          <h3>基本信息</h3>
          <div className="form-grid">
            <label>
              <span>策略名称</span>
              <input
                maxLength={120}
                onChange={(event) => updateDraft({ name: event.target.value })}
                value={draft.name}
              />
            </label>
            <label>
              <span>优先级</span>
              <input
                onChange={(event) => updateDraft({ priority: Number(event.target.value) })}
                type="number"
                value={draft.priority}
              />
            </label>
            <label>
              <span>重复提醒</span>
              <select
                onChange={(event) =>
                  updateDraft({ repeat_interval_seconds: Number(event.target.value) })
                }
                value={draft.repeat_interval_seconds}
              >
                {REPEAT_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </section>

        <SourceScopeEditor
          onChange={(source_scope) => updateDraft({ source_scope })}
          sources={sources}
          value={draft.source_scope}
        />

        <section className="editor-section">
          <div className="editor-section__head">
            <div>
              <h3>稳定字段 Matcher</h3>
              <p>支持 aggregation_rule_id、severity 和 group.&lt;label&gt;；来源只通过上方来源范围配置。</p>
            </div>
            <button
              className="secondary-btn"
              onClick={() =>
                updateDraft({
                  matchers: [
                    ...draft.matchers,
                    { field: "severity", operator: "=", value: "critical" },
                  ],
                })
              }
              type="button"
            >
              添加条件
            </button>
          </div>
          {draft.matchers.length === 0 ? (
            <div className="catchall-warning">
              显式兜底策略：空 matcher 会匹配所有尚未被前序策略命中的 Incident。
            </div>
          ) : (
            <div className="matcher-list">
              {draft.matchers.map((matcher, index) => (
                <div className="matcher-row" key={`${index}-${matcher.field}`}>
                  <input
                    list="notification-fields"
                    onChange={(event) => updateMatcher(index, { field: event.target.value })}
                    value={matcher.field}
                  />
                  <select
                    onChange={(event) =>
                      updateMatcher(index, { operator: event.target.value as MatcherOperator })
                    }
                    value={matcher.operator}
                  >
                    {OPERATORS.map((operator) => (
                      <option key={operator}>{operator}</option>
                    ))}
                  </select>
                  <input
                    onChange={(event) => updateMatcher(index, { value: event.target.value })}
                    value={matcher.value}
                  />
                  <button
                    onClick={() =>
                      updateDraft({ matchers: draft.matchers.filter((_, i) => i !== index) })
                    }
                    type="button"
                  >
                    移除
                  </button>
                </div>
              ))}
            </div>
          )}
          <datalist id="notification-fields">
            {STABLE_FIELDS.map((field) => (
              <option key={field} value={field} />
            ))}
          </datalist>
        </section>

        <section className="editor-section">
          <h3>目标通道</h3>
          <div className="check-grid">
            {channels.map((channel) => (
              <label className="check-card" key={channel.id}>
                <input
                  checked={draft.channel_ids.includes(channel.id)}
                  onChange={() =>
                    updateDraft({
                      channel_ids: draft.channel_ids.includes(channel.id)
                        ? draft.channel_ids.filter((id) => id !== channel.id)
                        : [...draft.channel_ids, channel.id],
                    })
                  }
                  type="checkbox"
                />
                <span>
                  <strong>{channel.name}</strong>
                  <small>
                    {channelStateLabel(channel.state)} ·{" "}
                    {channel.active_revision_id ? "配置已启用" : "未配置"}
                  </small>
                </span>
              </label>
            ))}
          </div>
          {channels.length === 0 && (
            <div className="placeholder-card">先在“通道”标签创建并启用一个飞书通道。</div>
          )}
        </section>

        <section className="editor-section">
          <div className="editor-section__head">
            <div>
              <h3>策略预览</h3>
              <p>不会写入数据库，也不会发送消息。</p>
            </div>
            <button
              className="secondary-btn"
              disabled={busy !== "" || draft.channel_ids.length === 0}
              onClick={() =>
                act(
                  "preview",
                  async () =>
                    setPreview(
                      await commands.previewNotificationPolicy(draft),
                    ),
                  "预览已更新。",
                )
              }
              type="button"
            >
              运行预览
            </button>
          </div>
          {preview ? (
            <div className="metric-grid">
              <div>
                <span>直接命中</span>
                <strong>{preview.direct_match_count}</strong>
              </div>
              <div>
                <span>被前序遮挡</span>
                <strong>{preview.shadowed_count}</strong>
              </div>
              <div>
                <span>最终分配</span>
                <strong>{preview.final_match_count}</strong>
              </div>
              <div>
                <span>无路由</span>
                <strong>{preview.unrouted_count}</strong>
              </div>
              <div>
                <span>当前 firing</span>
                <strong>{preview.firing_match_count}</strong>
              </div>
              <div>
                <span>禁用通道</span>
                <strong>{preview.disabled_channel_count}</strong>
              </div>
            </div>
          ) : (
            <div className="placeholder-card">修改后运行预览，确认优先级遮挡与无路由数量。</div>
          )}
        </section>

        <section className="editor-section">
          <div className="editor-section__head">
            <div>
              <h3>保存与启用</h3>
              <p>保存无副作用；启用必须重新计算影响并二次确认。</p>
            </div>
            <button
              className="primary-btn"
              disabled={busy !== "" || !draft.name.trim() || draft.channel_ids.length === 0}
              onClick={() => void save()}
              type="button"
            >
              保存
            </button>
          </div>
          {selected?.state === "DRAFT" && (
            <div className="activation-box">
              <label className="check-card">
                <input
                  checked={notifyExisting}
                  onChange={(event) => {
                    setNotifyExisting(event.target.checked);
                    setPrepared(null);
                  }}
                  type="checkbox"
                />
                <span>
                  <strong>启用时通知当前 firing</strong>
                  <small>默认关闭；开启后会按通道限流排队。</small>
                </span>
              </label>
              <div className="inline-actions">
                <button
                  className="secondary-btn"
                  disabled={busy !== ""}
                  onClick={() => void prepare()}
                  type="button"
                >
                  计算启用影响
                </button>
                {prepared && (
                  <button
                    className="danger-primary-btn"
                    disabled={busy !== ""}
                    onClick={confirmActivation}
                    type="button"
                  >
                    {notifyExisting
                      ? `启用并通知当前 ${prepared.eligible_incident_count} 个事件`
                      : "启用，仅通知后续变化"}
                  </button>
                )}
              </div>
              {prepared && (
                <small>
                  确认令牌将在 {new Date(prepared.expires_at_epoch * 1000).toLocaleTimeString()} 过期。
                </small>
              )}
            </div>
          )}
        </section>

        {message && (
          <div className="toast-line toast-line--ok" role="status">
            {message}
          </div>
        )}
        {error && (
          <div className="toast-line toast-line--error" role="alert">
            {error}
          </div>
        )}
      </section>
    </div>
  );
}
