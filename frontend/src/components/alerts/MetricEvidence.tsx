/**
 * The metric section of an alert group's detail (F27 / CAP-12).
 *
 * **Evidence belongs to one member, not to the group.** Group members share
 * their grouping labels and nothing else — the grouping key cannot even contain
 * `alertname` — so there is no member that can stand in for the others. A single
 * member is selected for you as a convenience; it is never called
 * "representative", because it does not represent anything but itself.
 */
import { useEffect, useMemo, useState } from "react";

import { failureText, warningText } from "../../curveFormat";
import { useMetricEvidence } from "../../queries/metrics";
import type { AlertOut } from "../../types";
import { MetricCurve } from "./MetricCurve";
import { AlertInvestigation } from "./AlertInvestigation";

interface Props {
  members: AlertOut[];
}

export function MetricEvidence({ members }: Props) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [windowMode, setWindowMode] = useState<"RECENT" | "ONSET">("RECENT");

  // Default to the first member (already sorted by severity then recency by the
  // API) whenever the group changes. A single-member group needs no click.
  const memberIds = members.map((member) => member.id).join(",");
  useEffect(() => {
    setSelectedId(members.length > 0 ? members[0].id : null);
  }, [memberIds, members]);

  const selected = useMemo(
    () => members.find((member) => member.id === selectedId) ?? null,
    [members, selectedId],
  );

  const query = useMetricEvidence(selected?.id ?? null, windowMode);

  if (members.length === 0) return null;

  return (
    <section className="section metric-evidence" data-testid="metric-evidence">
      <header className="metric-evidence__head">
        <h3>指标</h3>
        {members.length > 1 ? (
          <label className="metric-evidence__member">
            <span>看哪一条告警的指标</span>
            <select
              value={selectedId ?? ""}
              onChange={(event) => setSelectedId(Number(event.target.value))}
            >
              {members.map((member) => (
                <option key={member.id} value={member.id}>
                  {member.alertname}
                  {member.labels?.instance ? ` · ${member.labels.instance}` : ""}
                  {member.labels?.pod ? ` · ${member.labels.pod}` : ""}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <div className="metric-evidence__actions">
          <button
            type="button"
            className={windowMode === "RECENT" ? "pill pill--on" : "pill"}
            onClick={() => setWindowMode("RECENT")}
          >
            最近
          </button>
          <button
            type="button"
            className={windowMode === "ONSET" ? "pill pill--on" : "pill"}
            onClick={() => setWindowMode("ONSET")}
          >
            触发阶段
          </button>
          <button
            type="button"
            className="link"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
          >
            {query.isFetching ? "取数中…" : "刷新"}
          </button>
        </div>
      </header>

      {query.isError ? (
        <p className="dim">读取指标失败：{String(query.error)}</p>
      ) : null}
      {query.isLoading ? <p className="dim">正在取指标…</p> : null}

      {query.data ? (
        <>
          {/* Warnings first and visibly *not* failures: the curves below are
              real, something about them was merely inferred. */}
          {query.data.warnings.length > 0 ? (
            <ul className="metric-evidence__warnings">
              {query.data.warnings.map((note, index) => (
                <li key={`${note.kind}-${index}`}>
                  <span className="pill pill--warn">提示</span> {warningText(note.kind)}
                  {note.subject ? <span className="dim"> · {note.subject}</span> : null}
                  {/* The safe error code is the only thing that tells HTTP_404
                      from TIMEOUT. Classifying failures and then hiding it makes
                      the taxonomy decorative. */}
                  {note.detail ? <code className="dim"> {note.detail}</code> : null}
                </li>
              ))}
            </ul>
          ) : null}

          {query.data.curves.map((curve) => (
            <MetricCurve
              key={curve.curve_id}
              curve={curve}
              alertLabels={selected?.labels ?? {}}
              alertStartsAt={query.data.alert_starts_at}
            />
          ))}

          {query.data.failures.length > 0 ? (
            <ul className="metric-evidence__failures">
              {query.data.failures.map((note, index) => (
                <li key={`${note.kind}-${index}`}>
                  <span className="pill pill--bad">画不出</span> {failureText(note.kind)}
                  {note.subject ? <span className="dim"> · {note.subject}</span> : null}
                  {note.detail ? <code className="dim"> {note.detail}</code> : null}
                </li>
              ))}
            </ul>
          ) : null}

          {query.data.curves.length === 0 && query.data.failures.length === 0 ? (
            <p className="dim">这条告警没有可画的指标。</p>
          ) : null}

          {/* Below the deterministic curves, never instead of them: the primary
              curve is the ground truth this feature is built on top of. */}
          {/* The legacy investigation reads these curves, so it comes after
              the deterministic evidence rather than replacing it. */}
          {selected ? <AlertInvestigation alertId={selected.id} /> : null}
        </>
      ) : null}
    </section>
  );
}
