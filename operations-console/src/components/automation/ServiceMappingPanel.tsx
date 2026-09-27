import { useEffect, useMemo, useRef, useState } from "react";
import { useEventSources } from "../../queries/catalog";
import {
  usePreviewServiceMappingRule,
  usePublishServiceMappingRule,
  useSaveServiceMappingRule,
  useServiceMappingRules,
  useServices,
} from "../../queries/services";
import { describeRequestFailure } from "../../requestError";
import type { MatcherOperator, ServiceMappingPreview, ServiceMappingRule, ServiceMappingRuleDraft } from "../../types";

const emptyDraft = (serviceId = 0): ServiceMappingRuleDraft => ({
  name: "",
  priority: 100,
  service_id: serviceId,
  enabled: true,
  source_ids: [],
  matchers: [{ label: "alertname", operator: "=", value: "" }],
});

function fromRule(rule: ServiceMappingRule): ServiceMappingRuleDraft {
  return {
    name: rule.name,
    priority: rule.priority,
    service_id: rule.service_id,
    enabled: rule.enabled,
    source_ids: [...rule.source_ids],
    matchers: rule.matchers.map((item) => ({ ...item })),
  };
}

export function ServiceMappingPanel() {
  const rulesQuery = useServiceMappingRules();
  const servicesQuery = useServices(false);
  const sourcesQuery = useEventSources();
  const save = useSaveServiceMappingRule();
  const previewMutation = usePreviewServiceMappingRule();
  const publish = usePublishServiceMappingRule();
  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);
  const services = useMemo(() => servicesQuery.data ?? [], [servicesQuery.data]);
  const sources = useMemo(() => sourcesQuery.data ?? [], [sourcesQuery.data]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = rules.find((item) => item.id === selectedId) ?? null;
  const [draft, setDraft] = useState<ServiceMappingRuleDraft>(emptyDraft());
  const [preview, setPreview] = useState<ServiceMappingPreview | null>(null);
  const [notice, setNotice] = useState("");
  const [dirty, setDirty] = useState(false);
  const nextMatcherId = useRef(1);
  const [matcherIds, setMatcherIds] = useState<number[]>([0]);

  useEffect(() => {
    setDraft(selected ? fromRule(selected) : emptyDraft(services[0]?.id ?? 0));
    setPreview(null);
    setMatcherIds((selected?.matchers ?? [{}]).map(() => nextMatcherId.current++));
    setDirty(false);
  }, [selected?.id, selected?.version, services[0]?.id]);

  const update = (patch: Partial<ServiceMappingRuleDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
    setPreview(null);
    setNotice("");
    setDirty(true);
  };
  const valid = draft.name.trim().length > 0
    && draft.service_id > 0
    && draft.matchers.length <= 20
    && draft.matchers.every((item) => item.label.trim() && item.value.length <= 512);

  const saveDraft = async () => {
    try {
      const saved = await save.mutateAsync({
        id: selected?.id ?? null,
        expectedVersion: selected?.version ?? null,
        draft: { ...draft, name: draft.name.trim() },
      });
      setSelectedId(saved.id);
      setPreview(null);
      setDirty(false);
      setNotice("规则草稿已保存，线上归属尚未改变。请先预览，再决定是否发布。");
    } catch (cause) {
      const failure = describeRequestFailure(cause, "映射规则草稿未保存");
      setNotice(`${failure.title}${failure.detail ? `：${failure.detail}` : ""}`);
    }
  };

  const runPreview = async () => {
    if (!selected) return;
    try {
      setPreview(await previewMutation.mutateAsync(selected.id));
      setNotice("预览只计算影响，不修改当前事件。确认结果后可发布。");
    } catch (cause) {
      const failure = describeRequestFailure(cause, "映射影响暂时无法计算");
      setNotice(`${failure.title}${failure.detail ? `：${failure.detail}` : ""}`);
    }
  };

  const publishRule = async () => {
    if (!selected || !preview) return;
    try {
      await publish.mutateAsync({ id: selected.id, expectedVersion: selected.version });
      setPreview(null);
      setNotice("规则已发布，后台正在重算未结束事件。人工归属和已冻结的确认时限不会被覆盖。");
    } catch (cause) {
      const failure = describeRequestFailure(cause, "映射规则未发布");
      setNotice(`${failure.title}${failure.detail ? `：${failure.detail}` : ""}`);
    }
  };

  return (
    <main className="config-page automation-config-page">
      <aside className="config-list">
        <div className="config-list__header"><div><span className="eyebrow">Service Mapping</span><h2>服务映射</h2></div><button className="primary-btn" onClick={() => { setSelectedId(null); setNotice(""); }} type="button">新建规则</button></div>
        <p className="config-list__hint">每条告警按优先级命中第一条规则；同一事件的成员指向不同服务时标记为“归属冲突”，不会拆分事件。</p>
        {rulesQuery.isError ? <div className="error">映射规则暂时无法读取；请稍后刷新。</div> : null}
        <div className="config-list__items">{rules.map((rule) => <button className={selectedId === rule.id ? "config-list-item config-list-item--active" : "config-list-item"} key={rule.id} onClick={() => { setSelectedId(rule.id); setNotice(""); }} type="button"><strong>{rule.name}</strong><small>优先级 {rule.priority} · {rule.service_name}</small><span className={`config-state${!rule.has_unpublished_changes && rule.published_version > 0 ? " config-state--ok" : ""}`}>{rule.published_version === 0 ? "未发布" : rule.has_unpublished_changes ? "有未发布修改" : `已发布 v${rule.published_version}`}</span></button>)}</div>
      </aside>
      <section className="config-workspace">
        <header className="config-title"><div><span className="eyebrow">{selected ? `Mapping #${selected.id}` : "New mapping"}</span><h2>{selected?.name ?? "新建服务映射"}</h2></div><span className="config-state">草稿与发布分离</span></header>
        <div className="boundary-note">优先级数字越小越先匹配。保存只更新草稿；预览不会写入；只有发布才启动后台重算。</div>
        <div className="service-form">
          <label><span>规则名称</span><input maxLength={120} onChange={(event) => update({ name: event.target.value })} placeholder="例如：Checkout 告警" value={draft.name} /></label>
          <label><span>匹配优先级</span><input min={0} max={1_000_000} onChange={(event) => update({ priority: Number(event.target.value) })} type="number" value={draft.priority} /><small>数字越小，越先于其他规则判断。</small></label>
          <label><span>归属服务</span><select onChange={(event) => update({ service_id: Number(event.target.value) })} value={draft.service_id}><option value={0}>请选择有效服务</option>{services.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}</select></label>
          <label className="toggle"><input checked={draft.enabled} onChange={(event) => update({ enabled: event.target.checked })} type="checkbox" /><span>发布后启用此规则</span></label>
        </div>
        <fieldset className="mapping-scope"><legend>监控来源范围</legend><small>不选择表示适用于全部来源。</small><div className="checkbox-grid">{sources.map((source) => <label key={source.id}><input checked={draft.source_ids.includes(source.id)} onChange={() => update({ source_ids: draft.source_ids.includes(source.id) ? draft.source_ids.filter((id) => id !== source.id) : [...draft.source_ids, source.id] })} type="checkbox" />{source.name}</label>)}</div></fieldset>
        <fieldset className="mapping-matchers"><legend>告警标签条件</legend>{draft.matchers.map((matcher, index) => <div className="mapping-matcher" key={matcherIds[index]}><input aria-label={`条件 ${index + 1} 标签`} onChange={(event) => update({ matchers: draft.matchers.map((item, current) => current === index ? { ...item, label: event.target.value } : item) })} placeholder="alertname" value={matcher.label} /><select aria-label={`条件 ${index + 1} 操作符`} onChange={(event) => update({ matchers: draft.matchers.map((item, current) => current === index ? { ...item, operator: event.target.value as MatcherOperator } : item) })} value={matcher.operator}><option value="=">等于</option><option value="!=">不等于</option><option value="=~">正则匹配</option><option value="!~">正则不匹配</option></select><input aria-label={`条件 ${index + 1} 值`} onChange={(event) => update({ matchers: draft.matchers.map((item, current) => current === index ? { ...item, value: event.target.value } : item) })} placeholder="CheckoutDown" value={matcher.value} /><button className="text-btn" disabled={draft.matchers.length === 1} onClick={() => { setMatcherIds((ids) => ids.filter((_id, current) => current !== index)); update({ matchers: draft.matchers.filter((_item, current) => current !== index) }); }} type="button">移除</button></div>)}<button className="secondary-btn" disabled={draft.matchers.length >= 20} onClick={() => { setMatcherIds((ids) => [...ids, nextMatcherId.current++]); update({ matchers: [...draft.matchers, { label: "", operator: "=", value: "" }] }); }} type="button">添加标签条件</button></fieldset>
        <div className="form-actions"><button className="secondary-btn" disabled={!valid || !dirty || save.isPending} onClick={() => void saveDraft()} type="button">保存草稿</button><button className="secondary-btn" disabled={!selected || dirty || previewMutation.isPending} onClick={() => void runPreview()} type="button">预览当前草稿影响</button><button className="primary-btn" disabled={!selected || !selected.has_unpublished_changes || dirty || !preview || publish.isPending} onClick={() => void publishRule()} type="button">发布并后台重算</button></div>
        {preview ? <div className="mapping-preview" role="status"><strong>预览结果</strong><dl><div><dt>直接命中的告警</dt><dd>{preview.matched_alert_count}</dd></div><div><dt>单一服务归属事件</dt><dd>{preview.mapped_occurrence_count}</dd></div><div><dt>归属冲突事件</dt><dd>{preview.ambiguous_occurrence_count}</dd></div><div><dt>仍未映射事件</dt><dd>{preview.unmapped_occurrence_count}</dd></div></dl></div> : null}
        {notice ? <div className="response-command-result" role="status">{notice}</div> : null}
      </section>
    </main>
  );
}
