import { useEffect, useMemo, useState } from "react";
import {
  useActivatePromptProfile,
  useCopyPromptProfile,
  usePreviewPromptProfile,
  usePromptProfiles,
  usePromptProfileRevisions,
  useTestPromptProfile,
  useUpdatePromptProfile,
} from "../../queries/promptProfiles";
import { useServices } from "../../queries/services";
import type { PromptGuidance, PromptProfile } from "../../types";

const EMPTY: PromptGuidance = {
  organization_context: "",
  investigation_focus: "",
  terminology: "",
  response_style: "",
};

const FIELDS: Array<{ key: keyof PromptGuidance; label: string; hint: string }> = [
  { key: "organization_context", label: "组织与系统背景", hint: "例如：结算服务由支付平台团队维护；核心依赖是订单与支付网关。" },
  { key: "investigation_focus", label: "调查关注顺序", hint: "例如：先核对错误率，再核对流量与依赖延迟。" },
  { key: "terminology", label: "内部术语", hint: "例如：Occurrence 在团队内称为“本次事件”。" },
  { key: "response_style", label: "表达偏好", hint: "例如：结论简洁，必须把反证和缺失证据单列。" },
];

function sameGuidance(left: PromptGuidance, right: PromptGuidance): boolean {
  return FIELDS.every(({ key }) => left[key] === right[key]);
}

export function PromptProfilesPanel() {
  const profilesQuery = usePromptProfiles();
  const servicesQuery = useServices();
  const profiles = profilesQuery.data ?? [];
  const [selectedId, setSelectedId] = useState("builtin-standard");
  const selected = profiles.find((item) => item.id === selectedId) ?? profiles[0] ?? null;
  const [guidance, setGuidance] = useState<PromptGuidance>(EMPTY);
  const [copyName, setCopyName] = useState("");
  const [globalDefault, setGlobalDefault] = useState(false);
  const [serviceIds, setServiceIds] = useState<number[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const copy = useCopyPromptProfile();
  const update = useUpdatePromptProfile();
  const preview = usePreviewPromptProfile();
  const test = useTestPromptProfile();
  const activate = useActivatePromptProfile();
  const revisions = usePromptProfileRevisions(selected?.id ?? null, historyOpen);
  const busy = copy.isPending || update.isPending || preview.isPending || test.isPending || activate.isPending;

  useEffect(() => {
    if (!selected) return;
    setGuidance(selected.guidance);
    setGlobalDefault(selected.is_global_default);
    setServiceIds(selected.service_ids);
  }, [selected?.id]);

  const selectProfile = (profileId: string) => {
    setSelectedId(profileId);
    setMessage("");
    setError("");
    preview.reset();
    setHistoryOpen(false);
  };

  const activeProfiles = useMemo(
    () => profiles.filter((item) => item.active_revision !== null),
    [profiles],
  );

  const saveDraftIfNeeded = async (profile: PromptProfile): Promise<PromptProfile> => {
    if (sameGuidance(guidance, profile.guidance)) return profile;
    return update.mutateAsync({ id: profile.id, guidance, revision: profile.revision });
  };

  const showError = (cause: unknown, fallback: string) => {
    setError(cause instanceof Error ? cause.message : fallback);
    setMessage("");
  };

  const copyStandard = async () => {
    if (!copyName.trim() || busy) return;
    try {
      const created = await copy.mutateAsync(copyName.trim());
      setSelectedId(created.id);
      setCopyName("");
      setMessage("已复制内置标准。现在只需填写需要定制的内容。 ");
    } catch (cause) {
      showError(cause, "提示配置未创建。 ");
    }
  };

  const runTest = async () => {
    if (!selected || selected.builtin || busy) return;
    try {
      const saved = await saveDraftIfNeeded(selected);
      const result = await test.mutateAsync({ id: saved.id, revision: saved.revision });
      setMessage(result.last_test_code === "OK" ? "本地契约测试通过；启用状态没有改变。" : "本地契约测试未通过；启用状态没有改变。 ");
      setError("");
    } catch (cause) {
      showError(cause, "本地契约测试未完成；启用状态没有改变。 ");
    }
  };

  const saveAndActivate = async () => {
    if (!selected || selected.builtin || busy) return;
    try {
      const saved = await saveDraftIfNeeded(selected);
      const activated = await activate.mutateAsync({
        id: saved.id,
        revision: saved.revision,
        globalDefault,
        serviceIds,
      });
      setSelectedId(activated.id);
      setMessage("已保存并启用；后续新调查会冻结这个版本。 ");
      setError("");
    } catch (cause) {
      showError(cause, "提示配置未启用。 ");
    }
  };

  const loadPreview = async () => {
    if (!selected || busy) return;
    try {
      const saved = selected.builtin ? selected : await saveDraftIfNeeded(selected);
      await preview.mutateAsync({ id: saved.id, revision: saved.revision });
      setMessage("已生成保存版本的分层预览。 ");
      setError("");
    } catch (cause) {
      showError(cause, "预览未生成。 ");
    }
  };

  return (
    <section className="model-settings prompt-profile-settings" aria-labelledby="prompt-profile-title">
      <header className="management-section__head model-settings__head">
        <div>
          <span className="eyebrow">AI investigation guidance</span>
          <h2 id="prompt-profile-title">提示配置</h2>
          <p>内置标准可以直接使用。只有团队背景、关注顺序、术语和表达偏好可编辑。</p>
        </div>
      </header>

      <div className="boundary-note">
        字段范围、只读工具、查询编译、证据引用、结构化输出、预算和无写边界由代码固定，不能在这里修改。
        测试只做本地契约检查，不调用模型，也不影响启用。
      </div>

      <div className="resource-split settings-split model-settings__split">
        <aside className="resource-list">
          {profiles.map((profile) => (
            <button
              className={`resource-row${selected?.id === profile.id ? " resource-row--active" : ""}`}
              key={profile.id}
              onClick={() => selectProfile(profile.id)}
              type="button"
            >
              <span>
                <strong>{profile.name}</strong>
                <small>版本 {profile.revision} · {profile.builtin ? "内置只读" : profile.active_revision === null ? "尚未启用" : profile.status === "ACTIVE" ? "已启用" : `已启用版本 ${profile.active_revision}；当前为草稿`}</small>
                <small>{profile.is_global_default ? "全局默认" : profile.service_ids.length ? `绑定 ${profile.service_ids.length} 个服务` : "未设为默认"}</small>
              </span>
            </button>
          ))}
          {profilesQuery.isError ? <div className="toast-line toast-line--error">提示配置列表暂时不可用。</div> : null}
        </aside>

        <div className="resource-editor">
          {selected?.builtin ? (
            <>
              <div className="editor-head">
                <div><span className="eyebrow">默认安全配置</span><h2>内置标准</h2></div>
              </div>
              <p className="muted-copy">不需要配置即可用于调查。要增加团队背景，请复制一份，不会修改内置标准。</p>
              <div className="form-grid form-grid--action-row">
                <label><span>新配置名称</span><input maxLength={120} onChange={(event) => setCopyName(event.target.value)} placeholder="例如：结算服务调查" value={copyName} /></label>
                <button className="primary-btn" disabled={busy || !copyName.trim()} onClick={() => void copyStandard()} type="button">复制为自定义</button>
              </div>
            </>
          ) : selected ? (
            <>
              <div className="editor-head">
                <div><span className="eyebrow">{selected.status === "ACTIVE" ? "已启用" : "可编辑"}</span><h2>{selected.name}</h2></div>
                <span className="pill">版本 {selected.revision}</span>
              </div>
              <div className="form-grid prompt-profile-fields">
                {FIELDS.map((field) => (
                  <label key={field.key}><span>{field.label}</span><textarea maxLength={2000} onChange={(event) => setGuidance((current) => ({ ...current, [field.key]: event.target.value }))} placeholder={field.hint} rows={3} value={guidance[field.key]} /></label>
                ))}
              </div>
              <div className="editor-section">
                <h3>用于哪些调查</h3>
                <label className="checkbox-line"><input checked={globalDefault} onChange={(event) => setGlobalDefault(event.target.checked)} type="checkbox" />设为全局默认</label>
                <div className="choice-grid">
                  {(servicesQuery.data ?? []).map((service) => (
                    <label className="checkbox-line" key={service.id}><input checked={serviceIds.includes(service.id)} onChange={(event) => setServiceIds((current) => event.target.checked ? [...current, service.id] : current.filter((id) => id !== service.id))} type="checkbox" />{service.name}</label>
                  ))}
                </div>
              </div>
              <div className="button-row">
                <button className="secondary-btn" disabled={busy} onClick={() => setGuidance(EMPTY)} type="button">恢复内置内容</button>
                <button className="secondary-btn" disabled={busy} onClick={() => void loadPreview()} type="button">查看分层预览</button>
                <button className="secondary-btn" disabled={busy} onClick={() => void runTest()} type="button">本地测试</button>
                <button className="primary-btn" disabled={busy} onClick={() => void saveAndActivate()} type="button">保存并启用</button>
              </div>
              <details
                className="prompt-revision-history"
                onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
              >
                <summary>查看版本记录与变更字段</summary>
                {revisions.isPending ? <p className="muted-copy">正在读取版本记录…</p> : null}
                {revisions.isError ? <p className="toast-line toast-line--error">版本记录暂时无法读取；当前编辑内容不受影响。</p> : null}
                <ol>
                  {revisions.data?.map((revision) => (
                    <li key={revision.revision}>
                      <div><strong>版本 {revision.revision}</strong><span>{revision.status === "ACTIVE" ? "已启用" : revision.status === "DRAFT" ? "草稿" : "已退役"}</span></div>
                      <small>
                        {revision.changed_fields.length === 0
                          ? "初始版本"
                          : `变更：${revision.changed_fields.map((key) => FIELDS.find((field) => field.key === key)?.label ?? key).join("、")}`}
                        {revision.created_at ? ` · ${new Date(revision.created_at).toLocaleString()}` : ""}
                      </small>
                    </li>
                  ))}
                </ol>
              </details>
            </>
          ) : <div className="empty">正在读取提示配置…</div>}

          {preview.data ? (
            <section className="editor-section prompt-preview" aria-label="提示配置预览">
              <h3>分层预览</h3>
              <p><strong>不可编辑安全层：</strong>{preview.data.safety_kernel.join("、")}</p>
              <p><strong>会发送的数据：</strong>{preview.data.egress_categories.join("、")}</p>
              <p><strong>当前偏好估算：</strong>{preview.data.estimated_tokens} tokens 上界；最大费用 UNKNOWN</p>
            </section>
          ) : null}
          {message ? <div className="toast-line toast-line--ok" role="status">{message}</div> : null}
          {error ? <div className="toast-line toast-line--error" role="alert">{error}</div> : null}
          <p className="muted-copy">当前可用于事故页单次选择的配置：{activeProfiles.length} 个。</p>
        </div>
      </div>
    </section>
  );
}
