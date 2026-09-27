/**
 * 历史 — what happened, as opposed to what is happening.
 *
 * The alerts page answers "what is still on fire"; this one answers "did this
 * happen before, and how did it end". Squeezing both onto one page is what F25's
 * recovered fold did, and the user's response to it was "不放在一起吧" (ADR 0008).
 *
 * Read-only by design: no control here changes a handling state. The verdict is
 * given on the alerts page and lands on the record from there.
 */
import { useCallback, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router";
import {
  HISTORY_CONCLUSIONS,
  parseHistorySelection,
  withHistorySelection,
  type HistoryConclusion,
} from "../appUrl";
import { OccurrenceDetail } from "../components/history/OccurrenceDetail";
import { OccurrenceList } from "../components/history/OccurrenceList";
import { useEventSources } from "../queries/catalog";
import { useIncidentOccurrences } from "../queries/occurrences";
import { parseSourceIds } from "../selection";

export function HistoryPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.toString();

  const selectedSourceIds = useMemo(() => parseSourceIds(search), [search]);
  const selection = useMemo(() => parseHistorySelection(search), [search]);

  const sourcesQuery = useEventSources();
  const pageQuery = useIncidentOccurrences({
    sourceIds: selectedSourceIds,
    conclusion: selection.conclusion,
    beforeId: selection.beforeId,
  });

  const sources = useMemo(() => sourcesQuery.data ?? [], [sourcesQuery.data]);
  const items = useMemo(() => pageQuery.data?.items ?? [], [pageQuery.data]);

  // Derived, never written back: opening someone else's link must not rewrite it.
  const selectedId = selection.occurrenceId ?? items[0]?.id ?? null;
  const selected = items.find((item) => item.id === selectedId) ?? null;
  // A link can name a record that is not on this page (a different filter, or a
  // cursor since moved). Say so instead of silently showing a different row.
  const linkedButMissing = selection.occurrenceId !== null && selected === null;

  const patch = useCallback(
    (next: URLSearchParams) => setSearchParams(next, { replace: true }),
    [setSearchParams],
  );

  const selectSource = useCallback(
    (sourceIds: string[]) => {
      const next = new URLSearchParams(search);
      next.delete("source_ids");
      sourceIds.forEach((sourceId) => next.append("source_ids", sourceId));
      // A narrower source set is a different result set; the cursor and the open
      // record both belong to the old one.
      next.delete("before_id");
      next.delete("occurrence");
      patch(next);
    },
    [search, patch],
  );

  return (
    <div className="history-page">
      <div className="split">
        <OccurrenceList
          conclusion={selection.conclusion}
          conclusions={HISTORY_CONCLUSIONS as readonly HistoryConclusion[]}
          error={pageQuery.isError || sourcesQuery.isError}
          hasCursor={selection.beforeId !== null}
          items={items}
          loading={pageQuery.isPending || sourcesQuery.isPending}
          nextBeforeId={pageQuery.data?.next_before_id ?? null}
          onConclusionChange={(conclusion) =>
            patch(withHistorySelection(search, { conclusion }))
          }
          onFirstPage={() => patch(withHistorySelection(search, { beforeId: null }))}
          onNextPage={(beforeId) => patch(withHistorySelection(search, { beforeId }))}
          onSelect={(occurrenceId) =>
            patch(withHistorySelection(search, { occurrenceId }))
          }
          onSourceFilterChange={selectSource}
          selectedId={selectedId}
          selectedSourceIds={selectedSourceIds}
          sources={sources}
        />
        <OccurrenceDetail
          linkedButMissing={linkedButMissing}
          occurrence={selected}
          onBackToList={() =>
            patch(withHistorySelection(search, { occurrenceId: null }))
          }
          onOpenIncident={(incidentId) => navigate(`/alerts?incident=${incidentId}`)}
        />
      </div>
    </div>
  );
}
