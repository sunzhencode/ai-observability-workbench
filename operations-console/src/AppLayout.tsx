/**
 * The shell around every page: sidebar, top bar, and whatever the URL selected.
 *
 * What used to live here — the top-level poll, the alert page's state, the
 * `health` prop threaded into four components — has moved to the query layer
 * and to `pages/`. The shell now owns only the frame.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Outlet, useLocation, useNavigate } from "react-router";
import { AppSidebar } from "./components/AppSidebar";
import { TopBar } from "./components/TopBar";
import { pathForView, resolveView, type AppView } from "./appUrl";
import { useHealth } from "./queries/health";
import { subscribePlatformEvents } from "./platformEvents";
import { isLoopbackHostname, NETWORK_TRUST_WARNING } from "./networkTrust";

const TITLE_BY_VIEW: Record<AppView, string> = {
  incidents: "Incidents",
  alerts: "告警",
  automation: "Automation",
  notifications: "通知",
  analytics: "Analytics",
  settings: "系统设置",
};

export function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const health = useHealth();

  useEffect(() => subscribePlatformEvents(queryClient), [queryClient]);

  const view = resolveView(location.pathname);

  return (
    <div className="app">
      <AppSidebar view={view} onViewChange={(next) => navigate(pathForView(next))} />
      <div className="app-main">
        {!isLoopbackHostname(window.location.hostname) ? (
          <div className="network-trust-warning" role="alert">
            {NETWORK_TRUST_WARNING}
          </div>
        ) : null}
        <TopBar
          health={health.data ?? null}
          healthError={health.isError}
          // Refreshing now means "everything the current page is subscribed
          // to", because nothing else is being fetched.
          onRefresh={() => void queryClient.invalidateQueries()}
          title={TITLE_BY_VIEW[view]}
        />
        <Outlet />
      </div>
    </div>
  );
}
