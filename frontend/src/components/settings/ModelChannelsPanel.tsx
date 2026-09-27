import { useEffect, useMemo, useState } from "react";
import { SecretField } from "../SecretField";
import {
  availableModelOptions,
  canSaveModelChannel,
  canTestModelChannel,
  emptyModelChannelForm,
  isInternalFakeModel,
  latestModelRevision,
  modelChannelDraft,
  modelChannelFormFrom,
  type ModelChannelForm,
  vendorForBaseUrl,
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

type Selection = number | "new" | null;

/**
 * What a revision state means to the person looking at it.
 *
 * `DRAFT` / `ACTIVE` / `RETIRED` are how the safety gate is written down; they
 * are not what a user came here to think about. The states themselves stay —
 * the gate is real and this domain keeps it — but the screen says where you are
 * in the three steps, which is the only thing the words need to convey.
 */
function stateLabel(state: string, testedOk: boolean): string {
  if (state === "ACTIVE") return "已启用";
  if (state === "RETIRED") return "已被新配置取代";
  return testedOk ? "已测试，待启用" : "已保存，待测试";
}

export function ModelChannelsPanel() {
  const channelsQuery = useModelChannels();
  const channels = useMemo(
    () => channelsQuery.data ?? [],
    [channelsQuery.data],
  );
  const [selection, setSelection] = useState<Selection>(null);
  const selected =
    typeof selection === "number"
      ? channels.find((item) => item.id === selection) ?? null
      : null;
  const revision = selected ? latestModelRevision(selected) : null;

  const [form, setForm] = useState<ModelChannelForm>(
    emptyModelChannelForm,
  );
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const vendorsQuery = useModelVendors();
  const runtime = useModelRuntime();
  const vendors = vendorsQuery.data ?? [];
  const [vendorId, setVendorId] = useState<string>("OPENAI");
  const selectedVendor = vendors.find((item) => item.id === vendorId) ?? null;
  const modelOptions = useMemo(
    () =>
      availableModelOptions(
        selectedVendor?.recommended_models ?? [],
        models,
        form.model,
      ),
    [form.model, models, selectedVendor],
  );

  // A saved channel's vendor is whichever preset its address matches; anything
  // else is CUSTOM. Derived rather than stored, so adding a preset later
  // reclassifies existing rows instead of leaving them mislabelled.
  useEffect(() => {
    if (vendors.length === 0) return;
    setVendorId(vendorForBaseUrl(form.baseUrl, vendors));
  }, [form.baseUrl, selected?.id, vendors.length]);

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
      return;
    }
    if (selected) {
      setForm(modelChannelFormFrom(selected));
      setModels([]);
    }
  }, [selection, selected?.id, revision?.id]);

  const showFailure = (cause: unknown, fallback: string) => {
    const failure = describeRequestFailure(cause, fallback);
    setError(failure.detail ?? failure.title);
  };

  const save = async () => {
    if (busy || !canSaveModelChannel(form)) return;
    setMessage("");
    setError("");
    try {
      const saved = await saveChannel.mutateAsync({
        channelId: selected?.id ?? null,
        expectedRevisionId: revision?.id ?? null,
        draft: modelChannelDraft(form),
      });
      setSelection(saved.id);
      setMessage("已保存，没有调用模型。测试连接是下一步可选操作。");
    } catch (cause) {
      showFailure(cause, "模型通道保存失败。");
    }
  };

  const test = async () => {
    if (!selected || !revision || revision.state !== "DRAFT" || busy) return;
    if (
      !window.confirm(
        "确认调用该模型服务完成结构化测试？只发送 synthetic Alert 和 synthetic fact，不发送真实告警；真实服务可能计费。",
      )
    ) {
      return;
    }
    setMessage("");
    setError("");
    try {
      const result = await channelAction.mutateAsync({
        action: "test",
        channelId: selected.id,
        revisionId: revision.id,
      });
      if ("ok" in result && result.ok) {
        // Enabled on the spot. The property that matters is "never call a
        // channel that has not passed a test", and a successful test satisfies
        // it — a second click bought nothing but a step to explain.
        await channelAction.mutateAsync({
          action: "activate",
          channelId: selected.id,
          revisionId: revision.id,
        });
        setMessage("测试通过，已启用。只有你之后显式点击时才会调用它。");
      } else if ("code" in result) {
        setError(`结构化测试失败：${modelFailureText(result.code, result.detail)}`);
      }
    } catch (cause) {
      showFailure(cause, "模型通道测试失败。");
    }
  };

  const loadModels = async () => {
    if (busy || !selected || !revision || runtime.data?.fake_mode) return;
    setMessage("");
    setError("");
    try {
      const result = await listModels.mutateAsync(revision.id);
      if (result.ok) {
        setModels(result.models);
        setMessage(
          result.models.length > 0
            ? `拉到 ${result.models.length} 个模型，在上面的下拉里选一个再保存。`
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
      });
      setMessage(`模型通道已${verb}。`);
    } catch (cause) {
      showFailure(cause, `模型通道${verb}失败。`);
    }
  };

  return (
    <section className="model-settings" aria-labelledby="model-settings-title">
      <header className="management-section__head model-settings__head">
        <div>
          <span className="eyebrow">AI model service</span>
          <h2 id="model-settings-title">模型服务</h2>
          <p>
            首次填写时直接选择模型。保存和测试都不会发送任何真实告警数据。
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
          <strong>当前是本地安全模式</strong>（默认本地监控栈或 <code>--mock</code>）：
          所有模型调用都会被替换成本地假客户端，
          <strong>不会真的发往你配置的服务</strong>。
          只有显式用 <code>./start.sh --configured</code> 重启后才允许测试外部模型服务。
        </p>
      ) : null}
      <div className="model-egress-warning">
        <strong>数据出境说明</strong>
        <span>
          后续调查会把所选告警内容（labels 可能含主机名、namespace、集群名）、指标数据和历史处置记录发往该模型服务。
          原始 payload 与 generatorURL 不会发送。每次真实调用前仍会展示预览并要求确认。
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
                  <small>{channel.enabled ? "允许显式调用" : "尚未启用"}</small>
                </span>
                <i
                  className={`resource-state resource-state--${
                    current?.state.toLowerCase() ?? "draft"
                  }`}
                >
                  {stateLabel(current?.state ?? "DRAFT", Boolean(current?.tested_ok_at))}
                </i>
              </button>
            );
          })}
          {channels.length === 0 && !channelsQuery.isLoading && (
            <div className="empty">还没有模型服务。添加操作不会调用外部网络。</div>
          )}
          {channelsQuery.isError && (
            <div className="toast-line toast-line--error" role="alert">
              模型服务列表加载失败。
            </div>
          )}
        </aside>

        <div className="resource-editor">
          <header className="editor-head">
            <div>
              <span className="eyebrow">
                {revision ? stateLabel(revision.state, Boolean(revision.tested_ok_at)) : "添加服务"}
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
            选服务商、选模型、填 key，然后保存。
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
                      // A preset overwrites whatever was typed before it: the
                      // form saying "OpenAI" while the request goes elsewhere
                      // is the worst kind of wrong, because the screen looks
                      // right.
                      baseUrl: preset && preset.base_url ? preset.base_url : "",
                      model: "",
                    }));
                    setModels([]);
                    setVendorId(id);
                  }}
                  value={vendorId}
                >
                  {vendors.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.label}
                    </option>
                  ))}
                </select>
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
                    {modelOptions.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
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
                    : "推荐项可在首次保存前直接选择；账号实际可用范围以服务商为准。"}
                </small>
              </label>
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
                保存
              </button>
              {!runtime.data?.fake_mode ? (
                <button
                  className="secondary-btn"
                  disabled={busy || !selected || !revision}
                  onClick={() => void loadModels()}
                  type="button"
                >
                  刷新账号可用模型
                </button>
              ) : null}
              {revision?.state === "DRAFT" && (
                <button
                  className="secondary-btn"
                  disabled={busy || !canTestModelChannel(form)}
                  onClick={() => void test()}
                  type="button"
                >
                  测试并启用
                </button>
              )}
            </div>
            <small>
              {runtime.data?.fake_mode
                ? "本地安全模式使用服务商推荐列表，不读取远端目录，也不展示测试客户端内部名称。"
                : "刷新目录使用上次保存的配置，只用于补充账号实际可用模型。"}
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
                  <dt>结构化测试</dt>
                  <dd>{revision.tested_ok_at ? "已通过" : "尚未通过"}</dd>
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
