import { useMemo, useState } from "react";
import { useEventSources, useRules } from "../../queries/catalog";
import {
  useCreateMaintenanceWindow,
  useEndMaintenanceWindow,
  useMaintenanceWindows,
} from "../../queries/noise";
import { useServices } from "../../queries/services";
import type { MaintenanceWindowDraft } from "../../types";

function localInput(value: Date): string {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function newWindow(): {
  scope: MaintenanceWindowDraft["scope_kind"];
  target: string;
  startsAt: string;
  endsAt: string;
  reason: string;
} {
  const now = new Date();
  now.setSeconds(0, 0);
  return {
    scope: "SOURCE",
    target: "",
    startsAt: localInput(now),
    endsAt: localInput(new Date(now.getTime() + 60 * 60_000)),
    reason: "",
  };
}

export function MaintenancePanel() {
  const sources = useEventSources();
  const services = useServices(false);
  const rules = useRules();
  const [includeEnded, setIncludeEnded] = useState(false);
  const windows = useMaintenanceWindows(includeEnded);
  const create = useCreateMaintenanceWindow();
  const end = useEndMaintenanceWindow();
  const [draft, setDraft] = useState(newWindow);
  const [notice, setNotice] = useState("");

  const options = useMemo(() => {
    if (draft.scope === "SOURCE") return (sources.data ?? []).map((item) => ({ value: item.id, label: item.name }));
    if (draft.scope === "SERVICE") return (services.data ?? []).map((item) => ({ value: String(item.id), label: item.name }));
    return (rules.data ?? []).map((item) => ({ value: String(item.id), label: item.name }));
  }, [draft.scope, rules.data, services.data, sources.data]);
  const start = new Date(draft.startsAt);
  const finish = new Date(draft.endsAt);
  const duration = finish.getTime() - start.getTime();
  const valid = draft.target !== "" && draft.reason.trim().length > 0
    && Number.isFinite(duration) && duration > 0 && duration <= 7 * 24 * 60 * 60_000;

  const submit = () => {
    if (!valid) return;
    const payload: MaintenanceWindowDraft = {
      scope_kind: draft.scope,
      source_id: draft.scope === "SOURCE" ? draft.target : null,
      service_id: draft.scope === "SERVICE" ? Number(draft.target) : null,
      aggregation_rule_id: draft.scope === "RULE" ? Number(draft.target) : null,
      starts_at: start.toISOString(),
      ends_at: finish.toISOString(),
      reason: draft.reason.trim(),
    };
    create.mutate(payload, {
      onSuccess: () => {
        setNotice("维护窗口已建立；窗口内只暂停匹配范围的平台协作通知。结束后不会补发已跳过消息。");
        setDraft(newWindow());
      },
    });
  };

  const targetName = (scope: string, sourceId: string | null, serviceId: number | null, ruleId: number | null) => {
    if (scope === "SOURCE") return sources.data?.find((item) => item.id === sourceId)?.name ?? sourceId;
    if (scope === "SERVICE") return services.data?.find((item) => item.id === serviceId)?.name ?? `服务 #${serviceId}`;
    return rules.data?.find((item) => item.id === ruleId)?.name ?? `规则 #${ruleId}`;
  };

  return (
    <section className="maintenance-workspace">
      <header className="management-hero">
        <div><span className="eyebrow">Planned work</span><h1>维护窗口</h1><p>为来源、服务或聚合规则建立一次性窗口。事件照常进入队列，原始告警始终可查看。</p></div>
      </header>
      <div className="maintenance-layout">
        <section className="config-section maintenance-form">
          <div className="config-section__head"><div><h2>建立窗口</h2><p>最长 7 天；原因必填，可随时提前结束。</p></div></div>
          <div className="rule-basics">
            <label><span>作用范围</span><select onChange={(event) => setDraft((value) => ({ ...value, scope: event.target.value as MaintenanceWindowDraft["scope_kind"], target: "" }))} value={draft.scope}><option value="SOURCE">整个告警来源</option><option value="SERVICE">一个服务</option><option value="RULE">一条聚合规则</option></select></label>
            <label><span>具体对象</span><select onChange={(event) => setDraft((value) => ({ ...value, target: event.target.value }))} value={draft.target}><option value="">请选择</option>{options.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
            <label><span>开始时间</span><input onChange={(event) => setDraft((value) => ({ ...value, startsAt: event.target.value }))} type="datetime-local" value={draft.startsAt} /></label>
            <label><span>结束时间</span><input onChange={(event) => setDraft((value) => ({ ...value, endsAt: event.target.value }))} type="datetime-local" value={draft.endsAt} /></label>
          </div>
          <label><span>维护原因</span><textarea maxLength={1000} onChange={(event) => setDraft((value) => ({ ...value, reason: event.target.value }))} placeholder="例如：数据库主从切换，预计短时触发连接告警" rows={3} value={draft.reason} /></label>
          {!valid && (draft.reason || draft.target) ? <small className="error">请选择对象、填写原因，并确保结束时间晚于开始时间且不超过 7 天。</small> : null}
          <div className="form-actions"><button className="primary-btn" disabled={!valid || create.isPending} onClick={submit} type="button">建立维护窗口</button></div>
          {create.error instanceof Error ? <small className="error" role="alert">窗口未建立：{create.error.message}</small> : null}
          {notice ? <small role="status">{notice}</small> : null}
        </section>

        <section className="config-section maintenance-list">
          <div className="config-section__head"><div><h2>{includeEnded ? "全部窗口" : "当前窗口"}</h2><p>结束窗口只影响未来消息，不回补窗口内已经跳过的消息。</p></div><label><input checked={includeEnded} onChange={(event) => setIncludeEnded(event.target.checked)} type="checkbox" /> 显示已结束</label></div>
          {windows.isPending ? <p>正在读取维护窗口…</p> : null}
          {windows.isError ? <div className="error">维护窗口暂时无法读取；事件和告警仍可正常查看。</div> : null}
          {windows.data?.map((item) => (
            <article className="maintenance-window-card" key={item.id}>
              <div><strong>{targetName(item.scope_kind, item.source_id, item.service_id, item.aggregation_rule_id)}</strong><span className={`pill ${item.status === "ACTIVE" ? "pill--warning" : ""}`}>{item.status === "ACTIVE" ? "生效中" : "已结束"}</span></div>
              <p>{item.reason}</p>
              <small>{new Date(item.starts_at).toLocaleString()} → {new Date(item.ends_at).toLocaleString()}</small>
              {item.status === "ACTIVE" ? (
                <button
                  className="secondary-btn"
                  disabled={end.isPending}
                  onClick={() => end.mutate(
                    { id: item.id, expectedVersion: item.version },
                    { onSuccess: () => setNotice("维护窗口已提前结束；未来的平台协作消息恢复发送，窗口内跳过的消息不会补发。") },
                  )}
                  type="button"
                >
                  提前结束
                </button>
              ) : null}
            </article>
          ))}
          {end.error instanceof Error ? <small className="error" role="alert">窗口未结束：{end.error.message}</small> : null}
          {!windows.isPending && !windows.isError && windows.data?.length === 0 ? <div className="empty">当前没有维护窗口。</div> : null}
        </section>
      </div>
    </section>
  );
}
