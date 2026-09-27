import { useEffect, useMemo, useState } from "react";
import {
  useArchiveService,
  useSaveService,
  useServices,
} from "../../queries/services";
import { describeRequestFailure } from "../../requestError";
import type { Service, ServiceCriticality, ServiceDraft } from "../../types";

const CRITICALITY: Record<ServiceCriticality, { label: string; sla: string }> = {
  TIER_0: { label: "Tier 0 · 核心交易", sla: "5 分钟" },
  TIER_1: { label: "Tier 1 · 关键服务", sla: "15 分钟" },
  TIER_2: { label: "Tier 2 · 重要服务", sla: "30 分钟" },
  TIER_3: { label: "Tier 3 · 一般服务", sla: "60 分钟" },
};

const emptyDraft = (): ServiceDraft => ({
  name: "",
  slug: "",
  criticality: "TIER_1",
  links: [],
});

function fromService(service: Service): ServiceDraft {
  return {
    name: service.name,
    slug: service.slug,
    criticality: service.criticality,
    links: [...service.links],
  };
}

export function ServiceCatalogPanel() {
  const servicesQuery = useServices(true);
  const save = useSaveService();
  const archive = useArchiveService();
  const services = useMemo(() => servicesQuery.data ?? [], [servicesQuery.data]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [draft, setDraft] = useState<ServiceDraft>(emptyDraft());
  const [linksText, setLinksText] = useState("");
  const [notice, setNotice] = useState("");
  const [archiveArmed, setArchiveArmed] = useState(false);
  const selected = services.find((item) => item.id === selectedId) ?? null;

  useEffect(() => {
    if (selected) {
      setDraft(fromService(selected));
      setLinksText(selected.links.join("\n"));
    } else {
      setDraft(emptyDraft());
      setLinksText("");
    }
    setArchiveArmed(false);
  }, [selected?.id, selected?.version]);

  const links = linksText.split("\n").map((item) => item.trim()).filter(Boolean);
  const valid = draft.name.trim().length > 0
    && /^[a-z][a-z0-9-]{0,62}$/.test(draft.slug.trim())
    && links.length <= 10
    && links.every((link) => link.startsWith("https://"));
  const readOnly = selected?.status === "ARCHIVED";

  const submit = async () => {
    setNotice("");
    try {
      const saved = await save.mutateAsync({
        id: selected?.id ?? null,
        expectedVersion: selected?.version ?? null,
        draft: { ...draft, name: draft.name.trim(), slug: draft.slug.trim(), links },
      });
      setSelectedId(saved.id);
      setNotice("服务资料已保存。确认时限只会用于之后新建的事件，不回算已有事件。");
    } catch (cause) {
      const failure = describeRequestFailure(cause, "服务资料未保存");
      setNotice(`${failure.title}${failure.detail ? `：${failure.detail}` : ""}`);
    }
  };

  const archiveSelected = async () => {
    if (!selected || !archiveArmed) {
      setArchiveArmed(true);
      return;
    }
    try {
      await archive.mutateAsync({ id: selected.id, expectedVersion: selected.version });
      setNotice("服务已归档。历史事件仍显示该服务，新的规则和人工归属不能再选择它。");
      setArchiveArmed(false);
    } catch (cause) {
      const failure = describeRequestFailure(cause, "服务未归档");
      setNotice(`${failure.title}${failure.detail ? `：${failure.detail}` : ""}`);
    }
  };

  return (
    <main className="config-page automation-config-page">
      <aside className="config-list">
        <div className="config-list__header">
          <div><span className="eyebrow">Service Catalog</span><h2>服务目录</h2></div>
          <button className="primary-btn" onClick={() => { setSelectedId(null); setNotice(""); }} type="button">新建服务</button>
        </div>
        <p className="config-list__hint">服务把告警事件映射到稳定业务对象；这里不配置用户、Owner 或值班人。</p>
        {servicesQuery.isError ? <div className="error">服务目录暂时无法读取；请稍后刷新。</div> : null}
        <div className="config-list__items">
          {services.map((service) => (
            <button
              className={selectedId === service.id ? "config-list-item config-list-item--active" : "config-list-item"}
              key={service.id}
              onClick={() => { setSelectedId(service.id); setNotice(""); }}
              type="button"
            >
              <strong>{service.name}</strong>
              <small>{CRITICALITY[service.criticality].label} · 确认 {CRITICALITY[service.criticality].sla}</small>
              <span className={`config-state${service.status === "ACTIVE" ? " config-state--ok" : ""}`}>
                {service.status === "ACTIVE" ? "有效" : "已归档"}
              </span>
            </button>
          ))}
        </div>
      </aside>

      <section className="config-workspace">
        <header className="config-title">
          <div><span className="eyebrow">{selected ? `Service #${selected.id}` : "New service"}</span><h2>{selected?.name ?? "新建服务"}</h2></div>
          <span className={`config-state${!readOnly ? " config-state--ok" : ""}`}>{readOnly ? "只读历史" : "可编辑"}</span>
        </header>
        <div className="boundary-note">
          服务等级决定新事件首次可信发现时冻结的确认时限。修改等级、规则重算或人工改归属，都不会追溯修改已开始的计时。
        </div>
        <div className="service-form">
          <label><span>服务名称</span><input disabled={readOnly} maxLength={120} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如：Checkout API" value={draft.name} /></label>
          <label><span>稳定标识</span><input disabled={readOnly} maxLength={63} onChange={(event) => setDraft({ ...draft, slug: event.target.value })} placeholder="checkout-api" value={draft.slug} /><small>小写字母开头，只使用小写字母、数字和连字符。</small></label>
          <label><span>服务等级与确认时限</span><select disabled={readOnly} onChange={(event) => setDraft({ ...draft, criticality: event.target.value as ServiceCriticality })} value={draft.criticality}>{Object.entries(CRITICALITY).map(([value, item]) => <option key={value} value={value}>{item.label} · {item.sla}</option>)}</select></label>
          <label className="service-form__wide"><span>操作手册与仪表盘链接（选填，每行一个）</span><textarea disabled={readOnly} onChange={(event) => setLinksText(event.target.value)} placeholder="https://runbooks.example/checkout" rows={5} value={linksText} /><small>只保存 HTTPS 链接；平台不会主动访问或执行其中内容。</small></label>
        </div>
        {!readOnly ? <div className="form-actions"><button className="primary-btn" disabled={!valid || save.isPending} onClick={() => void submit()} type="button">保存服务资料</button>{selected ? <button className={archiveArmed ? "danger-primary-btn" : "secondary-btn"} disabled={archive.isPending} onClick={() => void archiveSelected()} type="button">{archiveArmed ? "再次确认归档" : "归档服务"}</button> : null}</div> : null}
        {notice ? <div className="response-command-result" role="status">{notice}</div> : null}
      </section>
    </main>
  );
}
