/**
 * Addressable Incident Operations views during the replacement sequence.
 *
 * The operations information architecture fixes six primary questions: Incidents,
 * Alerts, Automation, Notifications, Analytics and Settings. History remains
 * an addressable compatibility route reached from Analytics rather than a
 * seventh top-level destination.
 *
 * An unknown path redirects to Incident Queue rather than showing a 404: every path in
 * this app is one we wrote, so an unreadable one is a stale link, and dropping
 * the user somewhere usable beats a dead end.
 */
import { createBrowserRouter, Navigate } from "react-router";
import type { ComponentType } from "react";
import { AppLayout } from "./AppLayout";
import { pathForView } from "./appUrl";

const lazyPage = <TModule extends Record<string, unknown>, TKey extends keyof TModule>(
  importer: () => Promise<TModule>,
  component: TKey,
) => async () => ({ Component: (await importer())[component] as ComponentType });

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    HydrateFallback: () => <div className="route-loading">正在加载工作台…</div>,
    children: [
      { index: true, element: <Navigate replace to={pathForView("incidents")} /> },
      {
        path: "incidents/:occurrenceId?",
        lazy: lazyPage(() => import("./pages/IncidentsPage"), "IncidentsPage"),
      },
      {
        path: "alerts",
        lazy: lazyPage(() => import("./pages/AlertsPage"), "AlertsPage"),
      },
      {
        path: "automation/:tab?",
        lazy: lazyPage(() => import("./pages/AutomationPage"), "AutomationPage"),
      },
      // The optional segment is the tab: /notifications alone lands on 策略.
      {
        path: "notifications/:tab?",
        lazy: lazyPage(
          () => import("./pages/NotificationsPage"),
          "NotificationsPage",
        ),
      },
      {
        path: "analytics",
        lazy: lazyPage(() => import("./pages/AnalyticsPage"), "AnalyticsPage"),
      },
      // Compatibility detail route. History is still addressable, but its
      // primary entry is now the Analytics page rather than a seventh nav item.
      {
        path: "history",
        lazy: lazyPage(() => import("./pages/HistoryPage"), "HistoryPage"),
      },
      // The optional segment is the tab: /settings alone lands on 数据源.
      // 模型服务 is a tab rather than a section under the source list — it is a
      // different kind of thing with its own encrypted, revisioned lifecycle.
      {
        path: "settings/:tab?",
        lazy: lazyPage(() => import("./pages/SettingsPage"), "SettingsPage"),
      },
      { path: "*", element: <Navigate replace to={pathForView("incidents")} /> },
    ],
  },
]);
