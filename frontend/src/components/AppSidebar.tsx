// The view list belongs to the URL contract now, not to the sidebar: two
// copies of it could disagree about which views exist.
import type { AppView } from "../appUrl";

interface Props {
  view: AppView;
  onViewChange: (view: AppView) => void;
}

export function AppSidebar({ view, onViewChange }: Props) {
  return (
    <aside className="app-sidebar">
      <div className="app-sidebar__brand">
        <span className="brand__mark" aria-hidden="true">
          <i />
          <i />
          <i />
          <i />
        </span>
        <div>
          <strong>Alert Workbench</strong>
          <span>本地告警与通知工作台</span>
        </div>
      </div>

      <nav className="side-nav" aria-label="主导航">
        <button
          className={view === "alerts" ? "side-nav__active" : ""}
          onClick={() => onViewChange("alerts")}
          type="button"
        >
          <span aria-hidden="true">◉</span>
          <span>
            <strong>告警</strong>
            <small>规则运行结果</small>
          </span>
        </button>
        <button
          className={view === "rules" ? "side-nav__active" : ""}
          onClick={() => onViewChange("rules")}
          type="button"
        >
          <span aria-hidden="true">⌘</span>
          <span>
            <strong>聚合规则</strong>
            <small>匹配与分组定义</small>
          </span>
        </button>
        <button
          className={view === "notifications" ? "side-nav__active" : ""}
          onClick={() => onViewChange("notifications")}
          type="button"
        >
          <span aria-hidden="true">◈</span>
          <span>
            <strong>通知</strong>
            <small>策略、通道与投递</small>
          </span>
        </button>
        <button
          className={view === "history" ? "side-nav__active" : ""}
          onClick={() => onViewChange("history")}
          type="button"
        >
          <span aria-hidden="true">◷</span>
          <span>
            <strong>历史</strong>
            <small>过去发生过什么</small>
          </span>
        </button>
        <button
          className={view === "settings" ? "side-nav__active" : ""}
          onClick={() => onViewChange("settings")}
          type="button"
        >
          <span aria-hidden="true">⚙</span>
          <span>
            <strong>系统设置</strong>
            <small>数据源与模型服务</small>
          </span>
        </button>
      </nav>

      <div className="app-sidebar__foot">
        MONITOR READ ONLY · NOTIFY CONTROLLED
      </div>
    </aside>
  );
}
