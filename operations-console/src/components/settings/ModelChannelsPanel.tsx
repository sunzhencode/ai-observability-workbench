import { useEffect, useMemo, useState } from "react";
import { SecretField } from "../SecretField";
import {
  canSaveModelChannel,
  emptyModelChannelForm,
  filterDiscoveredModels,
  latestModelRevision,
  modelChannelDraft,
  modelChannelFormFrom,
  type ModelChannelForm,
  isInternalFakeModel,
  modelOptionGroups,
} from "../../modelChannelDraft";
import {
  useModelChannelAction,
  useModelChannels,
  useSaveModelChannel,
  useListModelChannelModels,
  useModelVendors,
  useModelRuntime,
} from "../../queries/modelChannels";
import { modelFailureText } from "../../modelFailure";
import { describeRequestFailure } from "../../requestError";
import {
  modelTestButtonText,
  modelTestFeedback,
  type ModelTestAttempt,
} from "../../modelTestFeedback";

type Selection = string | null;

function testLabel(code: string | null | undefined): string {
  if (code === "OK") return "成功";
  if (code) return "失败";
  return "未测试";
}

export function ModelChannelsPanel() {
  const channelsQuery = useModelChannels();
  const channels = useMemo(
    () => channelsQuery.data ?? [],
    [channelsQuery.data],
  );
  const [selection, setSelection] = useState<Selection>(null);
  const selected =
    selection !== null && selection !== "new"
      ? channels.find((item) => item.id === selection) ?? null
      : null;
  const revision = selected ? latestModelRevision(selected) : null;

  const [form, setForm] = useState<ModelChannelForm>(
    emptyModelChannelForm,
  );
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [modelFilter, setModelFilter] = useState("");
  const [testAttempt, setTestAttempt] = useState<ModelTestAttempt | null>(null);
  const vendorsQuery = useModelVendors();
  const runtime = useModelRuntime();
  const vendors = vendorsQuery.data ?? [];
  const vendorId = form.providerId;
  const selectedVendor = vendors.find((item) => item.id === vendorId) ?? null;
  const modelGroups = useMemo(
    () => modelOptionGroups(
      selectedVendor?.recommended_models ?? [],
      filterDiscoveredModels(models, modelFilter),
      form.model,
    ),
    [form.model, modelFilter, models, selectedVendor],
  );

  const saveChannel = useSaveModelChannel();
  const channelAction = useModelChannelAction();
  const listModels = useListModelChannelModels();
  const busy =
    saveChannel.isPending || channelAction.isPending || listModels.isPending;

  useEffect(() => {
    if (selection === null && channels.length > 0) {
      setSelection(channels[0].id);
    }
  }, [channels, selection]);

  useEffect(() => {
    if (selection === "new") {
      setForm(emptyModelChannelForm());
      setModels([]);
      setModelFilter("");
      setTestAttempt(null);
      return;
    }
    if (selected) {
      setForm(modelChannelFormFrom(selected));
      setModels([]);
      setModelFilter("");
      setTestAttempt(null);
    }
  }, [selection, selected?.id, revision?.id]);

  const showFailure = (cause: unknown, fallback: string) => {
    const failure = describeRequestFailure(cause, fallback);
    setError(failure.detail ?? failure.title);
  };

  const save = async () => {
    if (busy || !canSaveModelChannel(form)) return;
    const wasEnabled = selected?.enabled ?? false;
    setMessage("");
    setError("");
    try {
      const saved = await saveChannel.mutateAsync({
        channelId: selected?.id ?? null,
        expectedRevisionId: revision?.version ?? null,
        draft: modelChannelDraft(form),
      });
      setSelection(saved.id);
      setMessage(
        wasEnabled
          ? "已保存修改，服务保持启用。测试连接是可选诊断。"
          : "已保存并启用。测试连接是可选诊断，不影响启用状态。",
      );
    } catch (cause) {
      showFailure(cause, "模型通道保存失败。");
    }
  };

  const test = async () => {
    if (!selected || !revision || busy) return;
    setMessage("");
    setError("");
    setTestAttempt({ state: "pending" });
    try {
      const result = await channelAction.mutateAsync({
        action: "test",
        channelId: selected.id,
        revisionId: revision.id,
        revisionNo: revision.version,
      });
      if ("ok" in result && result.ok) {
        setTestAttempt({ state: "success" });
      } else if ("code" in result) {
        setTestAttempt({
          state: "failure",
          code: result.code,
          detail: result.detail,
        });
      }
    } catch (cause) {
      const failure = describeRequestFailure(cause, "模型通道测试未完成");
      setTestAttempt({
        state: "request-failure",
        text: failure.detail === null
          ? failure.title
          : `${failure.title}：${failure.detail}`,
      });
    }
  };

  const loadModels = async () => {
    if (busy || !selected || runtime.data?.fake_mode) return;
    setMessage("");
    setError("");
    try {
      const result = await listModels.mutateAsync(selected.id);
      if (result.ok) {
        setModels(result.models);
        setMessage(
          result.models.length > 0
            ? `账号返回 ${result.models.length} 个模型。它们只代表账号可见；选择后可做远程兼容性测试。`
            : "这个服务没有返回任何模型。",
        );
      } else {
        setModels([]);
        setError(`拉取模型失败：${modelFailureText(result.code, result.detail)}`);
      }
    } catch (cause) {
      showFailure(cause, "拉取模型失败。");
    }
  };


  const toggle = async () => {
    if (!selected || !revision || busy) return;
    const action = selected.enabled ? "disable" : "enable";
    const verb = selected.enabled ? "停用" : "重新启用";
    if (!window.confirm(`确认${verb}「${selected.name}」？`)) return;
    setMessage("");
    setError("");
    try {
      await channelAction.mutateAsync({
        action,
        channelId: selected.id,
        revisionId: revision.id,
        revisionNo: revision.version,
      });
      setMessage(`模型通道已${verb}。`);
    } catch (cause) {
      showFailure(cause, `模型通道${verb}失败。`);
    }
  };

  const visibleTestFeedback = revision
    ? modelTestFeedback(
        testAttempt,
        revision.model,
        revision.last_test_code,
        revision.last_tested_at,
        runtime.data?.fake_mode ?? false,
      )
    : null;

  return (
    <section className="model-settings" aria-labelledby="model-settings-title">
      <header className="management-section__head model-settings__head">
        <div>
          <span className="eyebrow">AI model service</span>
          <h2 id="model-settings-title">模型服务</h2>
          <p>
            填写后一次保存并启用。测试连接是可选诊断，只返回结果，不影响启用状态。
          </p>
        </div>
        <button
          className="secondary-btn"
          onClick={() => setSelection("new")}
          type="button"
        >
          添加模型服务
        </button>
      </header>

      {runtime.data?.fake_mode ? (
        <p className="alert-note" role="status">
          <strong>当前是完全离线 Mock 模式</strong>（<code>--mock</code>）：
          所有模型调用都会被替换成本地假客户端，
          <strong>不会真的发往你配置的服务</strong>。
          请用不带 <code>--mock</code> 的正常本地模式，才可显式读取目录、测试或调查。
        </p>
      ) : null}
      <div className="model-egress-warning">
        <strong>数据出境说明</strong>
        <span>
          后续调查会把所选告警内容（labels 可能含主机名、namespace、集群名）、指标数据和历史处置记录发往该模型服务。
          原始 payload 与 generatorURL 不会发送。只有你显式点击“开始证据调查”才会调用模型。
        </span>
      </div>

      <div className="resource-split settings-split model-settings__split">
        <aside className="resource-list">
          {channels.map((channel) => {
            const current = latestModelRevision(channel);
            return (
              <button
                className={`resource-row${
                  selected?.id === channel.id ? " resource-row--active" : ""
                }`}
                key={channel.id}
                onClick={() => {
                  setSelection(channel.id);
                  setMessage("");
                  setError("");
                }}
                type="button"
              >
                <span>
                  <strong>{channel.name}</strong>
                  <small>
                    {current?.base_host || "未配置"} · {current?.model || "未配置模型"}
                  </small>
                  <small>{channel.enabled ? "已启用" : "已停用"} · 最近测试：{testLabel(current?.last_test_code)}</small>
                </span>
                <i
                  className={`resource-state resource-state--${
                    current?.state.toLowerCase() ?? "draft"
                  }`}
                >
                  {channel.enabled ? "已启用" : "已停用"}
                </i>
              </button>
            );
          })}
          {channels.length === 0 && !channelsQuery.isLoading && (
            <div className="empty">还没有模型服务。添加操作不会调用外部网络。</div>
          )}
          {channelsQuery.isError && (
            <div className="toast-line toast-line--error" role="alert">
              模型服务列表暂时不可用；请刷新后再编辑或测试通道。
            </div>
          )}
        </aside>

        <div className="resource-editor">
          <header className="editor-head">
            <div>
              <span className="eyebrow">
                {selected ? (selected.enabled ? "已启用" : "已停用") : "添加服务"}
              </span>
              <h2>{selected?.name ?? "新建模型服务"}</h2>
            </div>
            {selected && selected.active_revision_id && (
              <button
                className={selected.enabled ? "danger-btn" : "secondary-btn"}
                disabled={busy}
                onClick={() => void toggle()}
                type="button"
              >
                {selected.enabled ? "停用" : "重新启用"}
              </button>
            )}
          </header>

          <div className="boundary-note">
            选服务商、填模型名、填 key，然后一次保存并启用。
            {vendorId === "OPENAI" ? (
              <>OpenAI 使用 <strong>Responses API</strong>。</>
            ) : (
              <>该服务使用 <strong>OpenAI-compatible Chat Completions</strong>。</>
            )}
            名称创建后不可修改，地址必须是公网 HTTPS，安全护栏没有关闭开关。
          </div>

          <section className="editor-section">
            <h3>连接配置</h3>
            <div className="form-grid">
              <label>
                <span>名称</span>
                <input
                  disabled={selected !== null}
                  maxLength={120}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      name: event.target.value,
                    }))
                  }
                  value={form.name}
                />
              </label>
              <label>
                <span>服务商</span>
                {/* The address is a choice, not a typing task. It is the field
                    most likely to be got wrong and least possible to check: a
                    typo reads as "cannot connect", a missing /v1 reads as a
                    404, and both look like the service being broken. */}
                <select
                  onChange={(event) => {
                    const id = event.target.value;
                    const preset = vendors.find((item) => item.id === id);
                    setForm((current) => ({
                      ...current,
                      providerId: id as ModelChannelForm["providerId"],
                      // A preset overwrites whatever was typed before it: the
                      // form saying "OpenAI" while the request goes elsewhere
                      // is the worst kind of wrong, because the screen looks
                      // right.
                      baseUrl: preset && preset.base_url ? preset.base_url : "",
                      model: "",
                    }));
                    setModels([]);
                  }}
                  value={vendorId}
                >
                  {vendors.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.label}
                    </option>
                  ))}
                </select>
                {selectedVendor ? (
                  <small>
                    {selectedVendor.support_level === "REVIEWED" ? "已验证适配" : selectedVendor.support_level === "COMPATIBLE" ? "兼容协议" : "尽力兼容"}
                    {" · "}
                    {selectedVendor.protocol_profile === "RESPONSES" ? "Responses API" : "Chat Completions"}
                  </small>
                ) : null}
              </label>
              {vendorId === "CUSTOM" ? (
                <label className="form-grid__wide">
                  <span>Base URL</span>
                  <input
                    maxLength={2048}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        baseUrl: event.target.value,
                      }))
                    }
                    placeholder="https://model.example.com/v1"
                    value={form.baseUrl}
                  />
                </label>
              ) : (
                <label>
                  <span>地址</span>
                  {/* Shown, not editable: the user should be able to see where
                      their alert labels are about to go without being able to
                      break it by accident. */}
                  <input disabled readOnly value={form.baseUrl} />
                </label>
              )}
              <label>
                <span>模型名</span>
                {vendorId !== "CUSTOM" ? (
                  <select
                    onChange={(event) =>
                      setForm((current) => ({ ...current, model: event.target.value }))
                    }
                    value={form.model}
                  >
                    <option value="">选择一个模型</option>
                    {isInternalFakeModel(form.model) ? (
                      <option disabled value={form.model}>
                        旧本地测试占位值（请重新选择）
                      </option>
                    ) : null}
                    {modelGroups.map((group) => (
                      <optgroup key={group.label} label={group.label}>
                        {group.models.map((name) => (
                          <option key={name} value={name}>
                            {name}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                ) : (
                  <input
                    maxLength={256}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        model: event.target.value,
                      }))
                    }
                    placeholder="填写服务商提供的模型 ID"
                    value={form.model}
                  />
                )}
                <small>
                  {vendorId === "CUSTOM"
                    ? "自定义服务没有内置目录，请填写它提供的模型 ID。"
                    : "推荐项可在首次保存前选择；账号返回项只代表可见，不保证满足调查契约。"}
                </small>
              </label>
              {vendorId !== "CUSTOM" && models.length > 0 ? (
                <label>
                  <span>筛选账号返回模型</span>
                  <input
                    maxLength={128}
                    onChange={(event) => setModelFilter(event.target.value)}
                    placeholder="例如：gpt-5"
                    value={modelFilter}
                  />
                  <small>仅筛选账号返回的 {models.length} 项；当前值和推荐项始终保留。</small>
                </label>
              ) : null}
              <SecretField
                help="由模型服务提供。保存后只保留密文，页面和 API 都不会回显；留空表示不修改。"
                label="API key"
                onChange={(apiKey) =>
                  setForm((current) => ({ ...current, apiKey }))
                }
                required
                state={form.apiKey}
              />
            </div>
            <div className="inline-actions">
              <button
                className="primary-btn"
                disabled={busy || !canSaveModelChannel(form)}
                onClick={() => void save()}
                type="button"
              >
                {selected?.enabled ? "保存修改" : "保存并启用"}
              </button>
              {!runtime.data?.fake_mode ? (
                <button
                  className="secondary-btn"
                  disabled={busy || !selected}
                  onClick={() => void loadModels()}
                  type="button"
                >
                  读取账号可用模型
                </button>
              ) : null}
              {revision && (
                <button
                  className="secondary-btn"
                  disabled={busy}
                  onClick={() => void test()}
                  type="button"
                >
                  {modelTestButtonText(
                    testAttempt,
                    revision.last_test_code,
                    runtime.data?.fake_mode ?? false,
                  )}
                </button>
              )}
            </div>
            {visibleTestFeedback && (
              <div
                className={`model-test-feedback model-test-feedback--${visibleTestFeedback.tone}`}
                role={visibleTestFeedback.tone === "error" ? "alert" : "status"}
              >
                <strong>
                  {visibleTestFeedback.tone === "pending"
                    ? "测试进行中"
                    : visibleTestFeedback.tone === "success"
                      ? "测试成功"
                      : "测试未通过"}
                </strong>
                <span>{visibleTestFeedback.text}</span>
              </div>
            )}
            <small>
              {runtime.data?.fake_mode
                ? "完全离线 Mock 模式只使用推荐列表，不读取远端目录，也不展示测试客户端内部名称。"
                : "读取目录只发送认证后的模型列表请求，不发送告警或指标；远程测试使用上次保存的配置，以 synthetic 数据验证只读规划与结构化结论（共 2 次调用），结果不会改变启用状态。"}
            </small>
          </section>

          {revision && (
            <section className="editor-section">
              <h3>本次配置状态</h3>
              <dl className="model-config-summary">
                <div>
                  <dt>目标 host</dt>
                  <dd>{revision.base_host}</dd>
                </div>
                <div>
                  <dt>模型</dt>
                  <dd>{revision.model}</dd>
                </div>
                <div>
                  <dt>凭证</dt>
                  <dd>{revision.secret_configured ? "已加密保存" : "未配置"}</dd>
                </div>
                <div>
                  <dt>使用状态</dt>
                  <dd>{selected?.enabled ? "已启用" : "已停用"}</dd>
                </div>
                <div>
                  <dt>最近测试</dt>
                  <dd>{testLabel(revision.last_test_code)}</dd>
                </div>
              </dl>
            </section>
          )}

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
        </div>
      </div>
    </section>
  );
}
