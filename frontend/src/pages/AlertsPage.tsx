/**
 * 告警 — the workbench's default view.
 *
 * Everything the user is looking at lives in the URL: the source filter, the
 * selected rule, the selected incident. Nothing is mirrored into `useState`,
 * so there is no cascade of effects keeping copies in step — the old shell had
 * four levels of them, and re-ran the whole chain every 15 seconds.
 *
 * Defaults are *derived*, not written back. Opening `/alerts` picks a rule to
 * show without rewriting the address bar: a link that rewrites itself the
 * moment it is opened is no longer the link that was sent.
 */
import { useCallback, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { IncidentDetail } from "../components/IncidentDetail";
import { IncidentList } from "../components/IncidentList";
import { WatchdogOverview } from "../components/WatchdogOverview";
import { parseAlertsSelection, settingsPathForSource, withAlertsSelection } from "../appUrl";
import { useHealth } from "../queries/health";
import { useEventSources, useRules } from "../queries/catalog";
import { useHandlingChange, useIncident, useIncidents } from "../queries/incidents";
import {
  buildRuleOptions,
  filterVisibleIncidents,
  parseSourceIds,
  partitionIncidents,
  resolveSelectedRuleKey,
} from "../selection";
import type { HandlingState } from "../types";

export function AlertsPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.toString();

  // Browsing, not navigating: picking through rules and incidents should not
  // fill the back button, so every selection write replaces the entry.
  const replaceSearch = useCallback(
    (next: URLSearchParams) => setSearchParams(next, { replace: true }),
    [setSearchParams],
  );

  const selectedSourceIds = useMemo(() => parseSourceIds(search), [search]);
  const urlSelection = useMemo(() => parseAlertsSelection(search), [search]);

  const health = useHealth();
  const incidentsQuery = useIncidents(selectedSourceIds);
  const rulesQuery = useRules();
  const sourcesQuery = useEventSources();
  const handlingChange = useHandlingChange();

  const incidents = useMemo(() => incidentsQuery.data ?? [], [incidentsQuery.data]);
  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);
  const sources = useMemo(() => sourcesQuery.data ?? [], [sourcesQuery.data]);

  const listLoading = incidentsQuery.isPending || rulesQuery.isPending || sourcesQuery.isPending;
  const listError = incidentsQuery.isError || rulesQuery.isError || sourcesQuery.isError;

  const ruleOptions = useMemo(() => buildRuleOptions(rules, incidents), [rules, incidents]);

  // The URL wins while it names a rule that exists; otherwise the same
  // fallback the page has always used, computed rather than stored.
  const selectedRuleKey = useMemo(
    () => resolveSelectedRuleKey(urlSelection.ruleKey, ruleOptions),
    [urlSelection.ruleKey, ruleOptions],
  );

  // Two levels, one request: the rule narrows, then the source state decides
  // whether a group belongs in the main list or in the recovered fold below it
  // (F25). Filtering server-side with `?state=` would cost a second request to
  // get the fold's own numbers back.
  const visibleIncidents = useMemo(
    () => filterVisibleIncidents(incidents, selectedRuleKey),
    [incidents, selectedRuleKey],
  );
  const groups = useMemo(() => partitionIncidents(visibleIncidents), [visibleIncidents]);

  // A deep link keeps its incident even when the current filter does not list
  // it: the detail query below decides whether it exists, and an incident that
  // does exist should still open. Without a link, show the first row — falling
  // through to the fold when nothing is active, so the detail pane is never
  // blank while the list still has rows to offer.
  const selectedId =
    urlSelection.incidentId ?? groups.active[0]?.id ?? groups.recovered[0]?.id ?? null;
  const detailQuery = useIncident(selectedId);
  const linkedButMissing = urlSelection.incidentId !== null && detailQuery.isError;

  const selectSource = useCallback(
    (sourceIds: string[]) => {
      const next = new URLSearchParams(search);
      next.delete("source_ids");
      sourceIds.forEach((sourceId) => next.append("source_ids", sourceId));
      // Changing the filter can hide the open incident; drop it rather than
      // leave the detail pane showing something the list no longer contains.
      next.delete("incident");
      replaceSearch(next);
    },
    [search, replaceSearch],
  );

  const selectRule = useCallback(
    (ruleKey: string | null) =>
      replaceSearch(withAlertsSelection(search, { ruleKey, incidentId: null })),
    [search, replaceSearch],
  );

  const selectIncident = useCallback(
    (incidentId: number | null) =>
      replaceSearch(withAlertsSelection(search, { incidentId })),
    [search, replaceSearch],
  );

  const changeHandling = useCallback(
    async (incidentId: number, state: HandlingState, reason: string) => {
      await handlingChange.mutateAsync({ incidentId, state, reason });
    },
    [handlingChange],
  );

  const openSourceSettings = useCallback(
    (sourceId: string) => navigate(settingsPathForSource(sourceId)),
    [navigate],
  );

  return (
    <div className="alerts-page">
      <div className="split">
        <IncidentList
          incidents={groups.active}
          recoveredIncidents={groups.recovered}
          unsettledRecoveredCount={groups.unsettledRecoveredCount}
          ruleOptions={ruleOptions}
          selectedRuleKey={selectedRuleKey}
          selectedId={selectedId}
          selectedSourceIds={selectedSourceIds}
          sources={sources}
          loading={listLoading}
          error={listError}
          onRuleChange={selectRule}
          onSelect={selectIncident}
          onSourceFilterChange={selectSource}
        />
        {linkedButMissing ? (
          <section className="resource-editor">
            <div className="placeholder-card">
              <strong>找不到事件 #{urlSelection.incidentId}。</strong>
              <p>它可能已被保留期清理，或者这个链接来自另一个库。</p>
              <button className="secondary-btn" onClick={() => selectIncident(null)} type="button">
                回到列表
              </button>
            </div>
          </section>
        ) : (
          <IncidentDetail
            incident={detailQuery.data ?? null}
            loading={detailQuery.isFetching}
            onHandlingChange={changeHandling}
            onOpenSourceSettings={openSourceSettings}
          />
        )}
      </div>
      <WatchdogOverview health={health.data ?? null} healthError={health.isError} />
    </div>
  );
}
