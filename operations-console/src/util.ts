import type { Severity, SourceState } from "./types";

export function ageFrom(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function cardClass(sev: Severity, state: SourceState): string {
  const firing = state === "firing" ? " card--firing" : "";
  return `card card--${sev}${firing}`;
}

export function sevPill(sev: Severity): string {
  return `pill pill--${sev}`;
}

export function statePill(state: SourceState): string {
  return `pill pill--${state}`;
}
