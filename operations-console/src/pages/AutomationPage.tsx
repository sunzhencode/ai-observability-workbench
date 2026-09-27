import { useNavigate, useParams } from "react-router";
import {
  AUTOMATION_TABS,
  automationPath,
  parseAutomationTab,
} from "../appUrl";
import { ServiceCatalogPanel } from "../components/automation/ServiceCatalogPanel";
import { ServiceMappingPanel } from "../components/automation/ServiceMappingPanel";
import { MaintenancePanel } from "../components/automation/MaintenancePanel";
import { RuleWorkspace } from "./RulesPage";

const LABELS: Record<(typeof AUTOMATION_TABS)[number], string> = {
  services: "服务目录",
  mapping: "服务映射",
  groups: "分组规则",
  maintenance: "维护窗口",
  metrics: "指标模板",
};

export function AutomationPage() {
  const { tab } = useParams();
  const navigate = useNavigate();
  const active = parseAutomationTab(tab);
  return (
    <div className="rules-shell automation-shell">
      <div className="domain-tabs" role="tablist" aria-label="Automation">
        {AUTOMATION_TABS.map((name) => (
          <button
            aria-selected={name === active}
            className={name === active ? "domain-tab--active" : ""}
            key={name}
            onClick={() => navigate(automationPath(name))}
            role="tab"
            type="button"
          >{LABELS[name]}</button>
        ))}
      </div>
      {active === "services" ? <ServiceCatalogPanel /> : null}
      {active === "mapping" ? <ServiceMappingPanel /> : null}
      {active === "groups" ? <RuleWorkspace mode="groups" /> : null}
      {active === "maintenance" ? <MaintenancePanel /> : null}
      {active === "metrics" ? <RuleWorkspace mode="metrics" /> : null}
    </div>
  );
}
