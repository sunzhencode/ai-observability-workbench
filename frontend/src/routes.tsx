/**
 * The four primary views, each with an address.
 *
 * `AGENTS.md` fixes the sidebar at 告警 / 聚合规则 / 通知 / 历史 / 系统设置; this
 * table is that list, now written down somewhere a link can point at. 历史 was
 * added by F26 (ADR 0008) — a fifth entry needed an explicit boundary change,
 * because it answers a question no existing page could.
 *
 * An unknown path redirects to 告警 rather than showing a 404: every path in
 * this app is one we wrote, so an unreadable one is a stale link, and dropping
 * the user somewhere usable beats a dead end.
 */
import { createBrowserRouter, Navigate } from "react-router";
import { AppLayout } from "./AppLayout";
import { pathForView } from "./appUrl";
import { AlertsPage } from "./pages/AlertsPage";
import { HistoryPage } from "./pages/HistoryPage";
import { NotificationsPage } from "./pages/NotificationsPage";
import { RulesPage } from "./pages/RulesPage";
import { SettingsPage } from "./pages/SettingsPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate replace to={pathForView("alerts")} /> },
      { path: "alerts", element: <AlertsPage /> },
      // Same optional-segment shape as notifications: /rules alone lands on
      // 分组规则. F27's metric templates are a tab here, not a sixth entry.
      { path: "rules/:tab?", element: <RulesPage /> },
      // The optional segment is the tab: /notifications alone lands on 策略.
      { path: "notifications/:tab?", element: <NotificationsPage /> },
      { path: "history", element: <HistoryPage /> },
      // The optional segment is the tab: /settings alone lands on 数据源.
      // 模型服务 is a tab rather than a section under the source list — it is a
      // different kind of thing with its own DRAFT → tested → active gate.
      { path: "settings/:tab?", element: <SettingsPage /> },
      { path: "*", element: <Navigate replace to={pathForView("alerts")} /> },
    ],
  },
]);
