/**
 * 通道 — one channel is one destination, of one kind.
 *
 * F24 turned the kind into a choice. It is made once, at creation, and never
 * changes: a different kind is a different destination, the same as a different
 * group. The form shape follows the kind, and every credential field says what
 * it is and where to get it -- the page previously said neither, which is why
 * "where do I configure the Feishu token?" was a reasonable question with no
 * answer on screen.
 *
 * The two buttons that reach the outside world are still here and still gated:
 * a revision must pass an explicit test before it can be activated, and
 * disabling is a kill switch that suppresses rather than re-routes.
 */
import { useState } from "react";
import { api } from "../../api/client";
import { describeRequestFailure } from "../../requestError";
import {
  CHANNEL_KINDS,
  KIND_HINTS,
  KIND_LABELS,
  canSaveChannel,
  channelTargetSummary,
  describeRequirements,
  emptyConfig,
  parseRecipients,
  portForTlsMode,
} from "../../channelDraft";
import type {
  ChannelConfigDraft,
  ChannelKind,
  MentionMode,
  NotificationChannel,
  NotificationPolicy,
} from "../../types";
import {
  channelStateLabel,
  EVENTS,
  latestPolicies,
  latestRevision,
  revisionStateLabel,
} from "./shared";
import { SecretField } from "../SecretField";
import {
  emptySecretField,
  secretFieldSatisfied,
  secretUpdateFor,
  type SecretFieldState,
} from "../../secretField";

export function ChannelsPanel({
  channels,
  policies,
  selectedId,
  onSelect,
  refresh,
}: {
  channels: NotificationChannel[];
  policies: NotificationPolicy[];
  selectedId: number | null;
  onSelect: (channelId: number | null) => void;
  refresh: () => Promise<unknown>;
}) {
  const selected = channels.find((item) => item.id === selectedId) ?? null;
  const revision = selected ? latestRevision(selected) : null;
  const storedKind = (revision?.provider ?? "FEISHU_CUSTOM_BOT") as ChannelKind;

  const [name, setName] = useState("");
  const [kind, setKind] = useState<ChannelKind>("FEISHU_CUSTOM_BOT");
  const [config, setConfig] = useState<ChannelConfigDraft>(() =>
    emptyConfig("FEISHU_CUSTOM_BOT"),
  );
  const [secrets, setSecrets] = useState<Record<string, SecretFieldState>>({});
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  // The selection lives in the URL, so the draft is synchronised on identity
  // during render rather than from an effect.
  const identity = selected ? `${selected.id}@${revision?.version ?? 0}` : "new";
  if (loadedFor !== identity) {
    setLoadedFor(identity);
    if (selected) {
      setName(selected.name);
      setKind(storedKind);
      setConfig(emptyConfig(storedKind));
      setSecrets(storedSecretState(storedKind, revision));
    } else {
      setName("");
      setKind("FEISHU_CUSTOM_BOT");
      setConfig(emptyConfig("FEISHU_CUSTOM_BOT"));
      setSecrets(storedSecretState("FEISHU_CUSTOM_BOT", null));
    }
  }

  const secretOf = (field: string): SecretFieldState =>
    secrets[field] ?? emptySecretField(false);
  const setSecret = (field: string) => (next: SecretFieldState) =>
    setSecrets((current) => ({ ...current, [field]: next }));

  const chooseKind = (next: ChannelKind) => {
    setKind(next);
    setConfig(emptyConfig(next));
    setSecrets(storedSecretState(next, null));
    setMessage("");
    setError("");
  };

  /** The draft with every secret box turned back into what the API expects. */
  const configToSend = (): ChannelConfigDraft => {
    if (config.kind === "SMTP") {
      return { ...config, password: secretUpdateFor(secretOf("password")) };
    }
    if (config.kind === "GENERIC_WEBHOOK") {
      return {
        ...config,
        headers: secretUpdateFor(secretOf("headers")),
        signing_secret: secretUpdateFor(secretOf("signing_secret")),
      };
    }
    return {
      ...config,
      webhook: secretUpdateFor(secretOf("webhook")),
      signing_secret: secretUpdateFor(secretOf("signing_secret")),
    };
  };

  const requiredSecretsFilled =
    config.kind !== "FEISHU_CUSTOM_BOT"
    || secretFieldSatisfied(secretOf("webhook"), true);

  const act = async (key: string, action: () => Promise<unknown>, success: string) => {
    if (busy) return;
    setBusy(key);
    setMessage("");
    setError("");
    try {
      await action();
      setMessage(success);
      await refresh();
    } catch (cause) {
      const failure = describeRequestFailure(cause, "操作失败。");
      setError(failure.detail ?? failure.title);
    } finally {
      setBusy("");
    }
  };

  const save = () =>
    act(
      "save",
      async () => {
        const saved =
          selected && revision
            ? await api.updateNotificationChannel(
                selected.id,
                { name, config: configToSend() },
                revision.version,
              )
            : await api.createNotificationChannel({ name, config: configToSend() });
        onSelect(saved.id);
      },
      "通道更改已保存；没有发送任何消息。",
    );

  const toggleChannel = () => {
    if (!selected) return;
    const verb = selected.state === "ENABLED" ? "禁用" : "重新启用";
    const consequence =
      selected.state === "ENABLED"
        ? "这是安全 kill switch，既有 Incident 不会改投其它目标。"
        : "后续事件将恢复按策略投递。";
    if (!window.confirm(`${verb}通道 ${selected.name}？${consequence}`)) return;
    void act(
      "toggle",
      () =>
        selected.state === "ENABLED"
          ? api.disableNotificationChannel(selected.id)
          : api.enableNotificationChannel(selected.id),
      `通道已${verb}。`,
    );
  };

  const sendTest = () => {
    if (!revision) return;
    if (!window.confirm(`确认向「${selected?.name ?? name}」真实发送一条测试消息？`)) return;
    void act("test", () => api.testNotificationChannel(revision.id), "通道测试已完成。");
  };

  const activate = () => {
    if (!revision) return;
    if (!window.confirm(`确认启用 ${selected?.name}？既有 Incident 后续消息会使用此配置。`)) return;
    void act(
      "activate",
      () => api.activateNotificationChannel(revision.id, revision.version),
      "通道配置已启用。",
    );
  };

  return (
    <div className="resource-split">
      <aside className="resource-list">
        <div className="resource-list__head">
          <div>
            <span className="eyebrow">Channels</span>
            <h2>通道</h2>
          </div>
          <button className="secondary-btn" onClick={() => onSelect(null)} type="button">
            添加
          </button>
        </div>
        {channels.map((channel) => {
          const current = latestRevision(channel);
          return (
            <button
              className={`resource-row${selectedId === channel.id ? " resource-row--active" : ""}`}
              key={channel.id}
              onClick={() => onSelect(channel.id)}
              type="button"
            >
              <span>
                <strong>{channel.name}</strong>
                <small>
                  {KIND_LABELS[(current?.provider ?? "FEISHU_CUSTOM_BOT") as ChannelKind]} ·{" "}
                  {channelTargetSummary(channel)}
                </small>
                <small>
                  {channel.active_revision_id ? "配置已启用" : "未启用配置"} ·{" "}
                  {latestPolicies(policies).filter((item) =>
                    item.channel_ids.includes(channel.id),
                  ).length}{" "}
                  策略引用
                </small>
              </span>
              <i className={`resource-state resource-state--${channel.state.toLowerCase()}`}>
                {channelStateLabel(channel.state)}
              </i>
            </button>
          );
        })}
        {channels.length === 0 && (
          <div className="empty">还没有通知通道。保存不会发出任何消息。</div>
        )}
      </aside>

      <section className="resource-editor">
        <header className="editor-head">
          <div>
            <span className="eyebrow">
              {revision ? revisionStateLabel(revision.state) : "添加通道"}
            </span>
            <h2>{selected?.name ?? "添加通知通道"}</h2>
          </div>
          {selected && (
            <button
              className={selected.state === "ENABLED" ? "danger-btn" : "secondary-btn"}
              onClick={toggleChannel}
              type="button"
            >
              {selected.state === "ENABLED" ? "禁用通道" : "重新启用"}
            </button>
          )}
        </header>

        <div className="boundary-note">
          一个通道代表一个固定去处。<strong>渠道类型创建后不可更改</strong>
          ，换类型或换去处请新建通道；替换凭证只用于同一去处的轮换。
        </div>

        <section className="editor-section">
          <h3>渠道类型</h3>
          <div className="check-grid">
            {CHANNEL_KINDS.map((item) => (
              <label className="check-card" key={item}>
                <input
                  checked={(selected ? storedKind : kind) === item}
                  disabled={selected !== null}
                  name="channel-kind"
                  onChange={() => chooseKind(item)}
                  type="radio"
                />
                <span>
                  <strong>{KIND_LABELS[item]}</strong>
                  <small>{KIND_HINTS[item]}</small>
                </span>
              </label>
            ))}
          </div>
          <ul className="requirement-list">
            {describeRequirements(config).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>

        {config.kind === "GENERIC_WEBHOOK" && (
          <div className="catchall-warning">
            告警内容会被发往这个地址，其中包含 label 里的主机名、namespace 和集群名。
            只填你自己掌握的端点。
          </div>
        )}

        <section className="editor-section">
          <h3>基本信息</h3>
          <div className="form-grid">
            <label>
              <span>通道名称</span>
              <input
                disabled={selected !== null}
                maxLength={120}
                onChange={(event) => setName(event.target.value)}
                value={name}
              />
            </label>
          </div>

          {config.kind === "FEISHU_CUSTOM_BOT" && (
            <div className="form-grid">
              <SecretField
                help="整条地址粘进来即可；token 就是地址结尾那一段，没有单独的输入框。"
                label="Webhook 地址"
                onChange={setSecret("webhook")}
                required
                state={secretOf("webhook")}
              />
              <label>
                <span>安全关键词（对应飞书「自定义关键词」，可选）</span>
                <input
                  maxLength={64}
                  onChange={(event) =>
                    setConfig({ ...config, required_keyword: event.target.value || null })
                  }
                  value={config.required_keyword ?? ""}
                />
              </label>
              <SecretField
                help="飞书机器人若选了「签名校验」，把它给的密钥填这里；选了关键词就留空。"
                label="签名密钥"
                onChange={setSecret("signing_secret")}
                state={secretOf("signing_secret")}
              />
            </div>
          )}

          {config.kind === "SMTP" && (
            <div className="form-grid">
              <label>
                <span>SMTP 主机</span>
                <input
                  onChange={(event) => setConfig({ ...config, host: event.target.value })}
                  placeholder="smtp.example.com"
                  value={config.host}
                />
              </label>
              <label>
                <span>加密方式（端口随之固定）</span>
                <select
                  onChange={(event) => {
                    const tls_mode = event.target.value as "STARTTLS" | "TLS";
                    setConfig({ ...config, tls_mode, port: portForTlsMode(tls_mode) });
                  }}
                  value={config.tls_mode}
                >
                  <option value="STARTTLS">STARTTLS · 端口 587</option>
                  <option value="TLS">隐式 TLS · 端口 465</option>
                </select>
              </label>
              <label>
                <span>账号（留空表示不认证）</span>
                <input
                  autoComplete="username"
                  onChange={(event) => setConfig({ ...config, username: event.target.value })}
                  value={config.username}
                />
              </label>
              <SecretField
                help="多数邮箱服务要用「授权码」而不是登录密码。"
                label="密码 / 授权码"
                onChange={setSecret("password")}
                state={secretOf("password")}
              />
              <label>
                <span>发件人</span>
                <input
                  onChange={(event) => setConfig({ ...config, from_addr: event.target.value })}
                  placeholder="alert-bot@example.com"
                  value={config.from_addr}
                />
              </label>
              <label className="form-grid__wide">
                <span>收件人（逗号或换行分隔）</span>
                <textarea
                  onChange={(event) =>
                    setConfig({ ...config, to_addrs: parseRecipients(event.target.value) })
                  }
                  rows={3}
                  value={config.to_addrs.join("\n")}
                />
              </label>
              <label>
                <span>主题前缀（可选）</span>
                <input
                  maxLength={64}
                  onChange={(event) =>
                    setConfig({ ...config, subject_prefix: event.target.value })
                  }
                  placeholder="[prod]"
                  value={config.subject_prefix}
                />
              </label>
            </div>
          )}

          {config.kind === "GENERIC_WEBHOOK" && (
            <div className="form-grid">
              <label className="form-grid__wide">
                <span>HTTPS 端点</span>
                <input
                  onChange={(event) => setConfig({ ...config, url: event.target.value })}
                  placeholder="https://hooks.example.com/alert-workbench"
                  value={config.url}
                />
              </label>
              <SecretField
                help='形如 {"Authorization": "Bearer ..."}；整体按凭证处理，保存后不回显。'
                label="自定义请求头（JSON）"
                onChange={setSecret("headers")}
                state={secretOf("headers")}
              />
              <SecretField
                help="填了就会带上 X-Workbench-Signature，供接收方校验来源。"
                label="签名密钥（可选）"
                onChange={setSecret("signing_secret")}
                state={secretOf("signing_secret")}
              />
              <label>
                <span>超时（秒）</span>
                <input
                  max={30}
                  min={1}
                  onChange={(event) =>
                    setConfig({ ...config, timeout_seconds: Number(event.target.value) })
                  }
                  type="number"
                  value={config.timeout_seconds}
                />
              </label>
            </div>
          )}
        </section>

        {config.kind === "FEISHU_CUSTOM_BOT" && (
          <section className="editor-section">
            <h3>@ 对象与事件</h3>
            <div className="form-grid">
              <label>
                <span>@ 对象</span>
                <select
                  onChange={(event) =>
                    setConfig({
                      ...config,
                      mention_mode: event.target.value as MentionMode,
                      mention_users:
                        event.target.value === "USERS" ? config.mention_users : [],
                    })
                  }
                  value={config.mention_mode}
                >
                  <option value="NONE">不 @</option>
                  <option value="USERS">指定用户 open_id</option>
                  <option value="ALL">@all</option>
                </select>
              </label>
              {config.mention_mode === "USERS" && (
                <label className="form-grid__wide">
                  <span>open_id（逗号或换行分隔）</span>
                  <textarea
                    onChange={(event) =>
                      setConfig({
                        ...config,
                        mention_users: parseRecipients(event.target.value).map((open_id) => ({
                          open_id,
                        })),
                      })
                    }
                    rows={3}
                    value={config.mention_users.map((item) => item.open_id).join("\n")}
                  />
                </label>
              )}
            </div>
            <p className="sidebar-help">
              勾选的事件才会触发 @；一个都不勾时，即使选了 @all 也不会 @ 任何人。
            </p>
            <div className="check-grid">
              {EVENTS.map((event) => (
                <label className="check-card" key={event.value}>
                  <input
                    checked={Boolean(config.mention_on[event.value])}
                    onChange={(input) =>
                      setConfig({
                        ...config,
                        mention_on: {
                          ...config.mention_on,
                          [event.value]: input.target.checked,
                        },
                      })
                    }
                    type="checkbox"
                  />
                  <span>
                    <strong>{event.label}</strong>
                    <small>{event.value}</small>
                  </span>
                </label>
              ))}
            </div>
          </section>
        )}

        <section className="editor-section">
          <div className="editor-section__head">
            <div>
              <h3>保存、测试与启用</h3>
              <p>
                保存不出站。<strong>测试会真实发送一条消息</strong>
                到这个通道的去处，必须测试成功后才能启用。
              </p>
            </div>
            <button
              className="primary-btn"
              disabled={busy !== "" || !canSaveChannel(name, config) || !requiredSecretsFilled}
              onClick={save}
              type="button"
            >
              保存
            </button>
          </div>
          {revision?.state === "DRAFT" && (
            <div className="activation-box">
              <div className="inline-actions">
                <button
                  className="danger-primary-btn"
                  disabled={busy !== ""}
                  onClick={sendTest}
                  type="button"
                >
                  发送测试消息
                </button>
                <button
                  className="primary-btn"
                  disabled={busy !== "" || revision.last_test_status !== "SUCCESS"}
                  onClick={activate}
                  type="button"
                >
                  启用
                </button>
              </div>
              <small>
                最近测试：
                {revision.last_tested_at
                  ? `${revision.last_test_status} · ${revision.last_test_error_code ?? "OK"} · ${new Date(
                      revision.last_tested_at,
                    ).toLocaleString()}`
                  : "尚未测试"}
              </small>
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

/**
 * Which secret boxes already have something behind them.
 *
 * A saved value is never readable, so the box stays empty either way; this only
 * decides whether it says "已配置 · 留空表示不修改" and offers a clear checkbox.
 */
function storedSecretState(
  kind: ChannelKind,
  revision: { webhook_configured?: boolean; signing_secret_configured?: boolean;
    config_summary?: { secret_configured?: boolean; detail?: string } } | null,
): Record<string, SecretFieldState> {
  const summary = revision?.config_summary;
  if (kind === "SMTP") {
    return { password: emptySecretField(Boolean(summary?.secret_configured)) };
  }
  if (kind === "GENERIC_WEBHOOK") {
    return {
      headers: emptySecretField(Boolean(summary?.detail?.includes("已配置"))),
      signing_secret: emptySecretField(Boolean(summary?.secret_configured)),
    };
  }
  return {
    webhook: emptySecretField(Boolean(revision?.webhook_configured)),
    signing_secret: emptySecretField(Boolean(revision?.signing_secret_configured)),
  };
}
