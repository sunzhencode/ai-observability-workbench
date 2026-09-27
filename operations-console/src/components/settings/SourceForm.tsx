/**
 * The source editor: name + address, then everything else folded away.
 *
 * The information architecture here is untouched by F23 — the user deferred it
 * (`requirements.md` D5). Two behaviours inside are load-bearing and were each
 * a defect once:
 *
 *  - a secret is resolved by its endpoint's `position`, not by array order, so
 *    removing a middle endpoint must not renumber the survivors or they inherit
 *    the removed one's credential (`SYSTEM_SPEC.md` §11);
 *  - a failed connection test is a 200 with `ok:false`, and it has to stay
 *    visible (CAP-01.7) — it is reported by the caller, not swallowed here.
 */
import {
  advancedSummary,
  authSecret,
  authSummary,
  backupSummary,
  emptyEndpoint,
  emptyThanos,
  historySummary,
} from "../../sourceDraft";
import { sourceStatus, sourceStatusText, toneClassName } from "../../settingsStatus";
import type {
  EventSource,
  EventSourceAuthType,
  EventSourceCandidate,
  EventSourceEndpointDraft,
  EventSourceTestResult,
  EventSourceThanosDraft,
} from "../../types";
import { SecretField } from "../SecretField";
import { emptySecretField, secretUpdateFor } from "../../secretField";

function TestSummary({ test }: { test: EventSourceTestResult | null }) {
  if (!test) return null;
  return (
    <div className={`toast-line ${test.ok ? "toast-line--ok" : "toast-line--error"}`} role="status">
      测试结果：{test.code} ·{" "}
      {test.endpoints.map((item) => `#${item.position + 1} ${item.code}`).join(" / ")}
    </div>
  );
}

export function SourceForm({
  draft,
  setDraft,
  selected,
  editable,
  busy,
  testResult,
  saveDisabled,
  onTest,
  onSave,
  onDisable,
  onEnable,
  onArchive,
}: {
  draft: EventSourceCandidate;
  setDraft: (draft: EventSourceCandidate) => void;
  selected: EventSource | null;
  editable: boolean;
  busy: boolean;
  testResult: EventSourceTestResult | null;
  saveDisabled: boolean;
  onTest: () => void;
  onSave: () => void;
  onDisable: () => void;
  onEnable: () => void;
  onArchive: () => void;
}) {
  // Keyed on the row's own slot, not its array index: after a removal those
  // differ, and the hint would describe a different endpoint's credential.
  const secretConfigured = (index: number) => {
    const position = draft.endpoints[index]?.position;
    if (position === null || position === undefined) return false;
    return (
      selected?.config?.endpoints.find((item) => item.position === position)?.secret_configured ??
      false
    );
  };
  const thanosSecretConfigured = selected?.config?.thanos?.secret_configured ?? false;
  const backupEndpoints = draft.endpoints.slice(1);

  const updateEndpoint = (index: number, patch: Partial<EventSourceEndpointDraft>) => {
    setDraft({
      ...draft,
      endpoints: draft.endpoints.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    });
  };

  const updateThanos = (patch: Partial<EventSourceThanosDraft>) => {
    setDraft({ ...draft, thanos: { ...(draft.thanos ?? emptyThanos()), ...patch } });
  };

  const endpointFields = (index: number) => {
    const endpoint = draft.endpoints[index];
    const configured = secretConfigured(index);
    return (
      <div className="form-grid">
        <label>
          <span>认证</span>
          <select
            onChange={(event) => {
              const auth_type = event.target.value as EventSourceAuthType;
              updateEndpoint(index, {
                auth_type,
                username: auth_type === "BASIC" ? endpoint.username : "",
                secret: authSecret(auth_type, configured),
              });
            }}
            value={endpoint.auth_type}
          >
            <option value="NONE">无认证</option>
            <option value="BEARER">Bearer Token</option>
            <option value="BASIC">Basic 用户名/密码</option>
          </select>
        </label>
        {endpoint.auth_type === "BASIC" && (
          <label>
            <span>用户名</span>
            <input
              autoComplete="username"
              onChange={(event) => updateEndpoint(index, { username: event.target.value })}
              value={endpoint.username}
            />
          </label>
        )}
        {endpoint.auth_type !== "NONE" && (
          <SecretField
            help={
              endpoint.auth_type === "BASIC"
                ? "配对上面的用户名。"
                : "Alertmanager 前面若挂着带 token 的网关，填那个 token。"
            }
            label={endpoint.auth_type === "BASIC" ? "密码" : "Bearer Token"}
            onChange={(next) =>
              updateEndpoint(index, { secret: secretUpdateFor(next), secretField: next })
            }
            required={!configured}
            state={endpoint.secretField ?? emptySecretField(configured)}
          />
        )}
      </div>
    );
  };

  return (
    <>
      <header className="editor-head">
        <div>
          <span className="eyebrow">{selected ? selected.id : "新数据源"}</span>
          <h2>{selected?.name ?? "添加数据源"}</h2>
        </div>
        {selected && (
          <span className={`status-label ${toneClassName(sourceStatus(selected).tone)}`}>
            {sourceStatusText(selected)}
          </span>
        )}
      </header>

      <section className="editor-section">
        <div className="form-grid">
          <label className="form-grid__wide">
            <span>名称</span>
            <input
              disabled={!editable}
              maxLength={120}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              placeholder="例如 生产 Alertmanager"
              value={draft.name}
            />
          </label>
          <label className="form-grid__wide">
            <span>地址</span>
            <input
              disabled={!editable}
              onChange={(event) => updateEndpoint(0, { url: event.target.value })}
              placeholder="https://alertmanager.example"
              value={draft.endpoints[0]?.url ?? ""}
            />
          </label>
        </div>
        {selected && (
          <div className="inline-actions">
            <button className="secondary-btn" disabled={busy} onClick={onTest} type="button">
              测试连接
            </button>
            <small>测试只读，不是保存的前置条件。</small>
          </div>
        )}
        <TestSummary test={testResult} />
      </section>

      <details className="editor-section">
        <summary>
          备用地址（HA）
          <em>{backupSummary(draft)}</em>
        </summary>
        <p>同一来源的多个地址会按告警身份合并去重；全部成功才算一次完整轮询。</p>
        {backupEndpoints.map((endpoint, offset) => {
          const index = offset + 1;
          return (
            <div className="endpoint-card" key={index}>
              <div className="endpoint-card__head">
                <strong>备用地址 {index}</strong>
                <label className="switch-row">
                  <input
                    checked={endpoint.enabled}
                    disabled={!editable}
                    onChange={(event) => updateEndpoint(index, { enabled: event.target.checked })}
                    type="checkbox"
                  />
                  <span>启用</span>
                </label>
              </div>
              <div className="form-grid">
                <label className="form-grid__wide">
                  <span>地址</span>
                  <input
                    disabled={!editable}
                    onChange={(event) => updateEndpoint(index, { url: event.target.value })}
                    placeholder="https://alertmanager-2.example"
                    value={endpoint.url}
                  />
                </label>
              </div>
              {endpointFields(index)}
              <button
                className="secondary-btn"
                disabled={!editable}
                onClick={() =>
                  setDraft({
                    ...draft,
                    endpoints: draft.endpoints.filter((_, itemIndex) => itemIndex !== index),
                  })
                }
                type="button"
              >
                移除
              </button>
            </div>
          );
        })}
        <button
          className="secondary-btn"
          disabled={!editable || draft.endpoints.length >= 8}
          onClick={() => setDraft({ ...draft, endpoints: [...draft.endpoints, emptyEndpoint()] })}
          type="button"
        >
          + 添加备用地址
        </button>
      </details>

      <details className="editor-section">
        <summary>
          认证
          <em>{authSummary(draft)}</em>
        </summary>
        <p>凭证保存后永不回显，也永远不会出现在前端。</p>
        {endpointFields(0)}
      </details>

      <details className="editor-section">
        <summary>
          历史数据（选填）
          <em>{historySummary(draft)}</em>
        </summary>
        <p>只用于启动时回填一段历史。留空即不回填，其余功能不受影响。</p>
        <div className="form-grid">
          <label className="form-grid__wide">
            <span>Thanos 地址</span>
            <input
              disabled={!editable}
              onChange={(event) => updateThanos({ url: event.target.value })}
              placeholder="https://thanos.example"
              value={draft.thanos?.url ?? ""}
            />
          </label>
          <label>
            <span>认证</span>
            <select
              disabled={!editable}
              onChange={(event) => {
                const auth_type = event.target.value as "NONE" | "BEARER";
                updateThanos({
                  auth_type,
                  secret: authSecret(auth_type, thanosSecretConfigured),
                });
              }}
              value={draft.thanos?.auth_type ?? "NONE"}
            >
              <option value="NONE">无认证</option>
              <option value="BEARER">Bearer Token</option>
            </select>
          </label>
          <label>
            <span>超时（秒）</span>
            <input
              disabled={!editable}
              max={120}
              min={1}
              onChange={(event) => updateThanos({ timeout_seconds: Number(event.target.value) })}
              type="number"
              value={draft.thanos?.timeout_seconds ?? 15}
            />
          </label>
          {draft.thanos?.auth_type === "BEARER" && (
            <SecretField
              label="Bearer Token"
              onChange={(next) =>
                updateThanos({ secret: secretUpdateFor(next), secretField: next })
              }
              required={!thanosSecretConfigured}
              state={draft.thanos.secretField ?? emptySecretField(thanosSecretConfigured)}
            />
          )}
        </div>
      </details>

      <details className="editor-section">
        <summary>
          高级
          <em>{advancedSummary(draft)}</em>
        </summary>
        <div className="form-grid">
          <label>
            <span>轮询间隔（秒）</span>
            <input
              disabled={!editable}
              max={3600}
              min={5}
              onChange={(event) =>
                setDraft({ ...draft, poll_interval_seconds: Number(event.target.value) })
              }
              type="number"
              value={draft.poll_interval_seconds}
            />
          </label>
          <label>
            <span>恢复宽限（秒）</span>
            <input
              disabled={!editable}
              max={86400}
              min={0}
              onChange={(event) =>
                setDraft({ ...draft, resolution_grace_seconds: Number(event.target.value) })
              }
              type="number"
              value={draft.resolution_grace_seconds}
            />
          </label>
          <label>
            <span>并发上限</span>
            <input
              disabled={!editable}
              max={8}
              min={1}
              onChange={(event) =>
                setDraft({ ...draft, max_parallel_endpoints: Number(event.target.value) })
              }
              type="number"
              value={draft.max_parallel_endpoints}
            />
          </label>
          <label className="switch-row">
            <input
              checked={draft.watchdog_enabled}
              disabled={!editable}
              onChange={(event) => setDraft({ ...draft, watchdog_enabled: event.target.checked })}
              type="checkbox"
            />
            <span>启用 Watchdog 监测</span>
          </label>
          <label>
            <span>Watchdog alertname</span>
            <input
              disabled={!editable}
              maxLength={128}
              onChange={(event) => setDraft({ ...draft, watchdog_alertname: event.target.value })}
              value={draft.watchdog_alertname}
            />
          </label>
          <label>
            <span>身份 label</span>
            <input
              disabled={!editable}
              maxLength={128}
              onChange={(event) =>
                setDraft({ ...draft, watchdog_identity_label: event.target.value })
              }
              value={draft.watchdog_identity_label}
            />
          </label>
          <label>
            <span>missing after（秒）</span>
            <input
              disabled={!editable}
              max={86400}
              min={60}
              onChange={(event) =>
                setDraft({ ...draft, watchdog_missing_after_seconds: Number(event.target.value) })
              }
              type="number"
              value={draft.watchdog_missing_after_seconds ?? 90}
            />
          </label>
        </div>
      </details>

      <div className="form-actions">
        {selected && selected.lifecycle_state !== "ARCHIVED" && (
          <>
            {selected.lifecycle_state === "ENABLED" ? (
              <button className="danger-btn" disabled={busy} onClick={onDisable} type="button">
                停用
              </button>
            ) : (
              <button className="secondary-btn" disabled={busy} onClick={onEnable} type="button">
                启用
              </button>
            )}
            <button className="danger-btn" disabled={busy} onClick={onArchive} type="button">
              归档
            </button>
          </>
        )}
        {editable && (
          <button className="primary-btn" disabled={saveDisabled} onClick={onSave} type="button">
            保存并生效
          </button>
        )}
      </div>
    </>
  );
}
