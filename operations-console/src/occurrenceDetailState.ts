import { backgroundReadState } from "./backgroundReadState";

export type OccurrenceDetailState =
  | "LOADING"
  | "READY"
  | "STALE"
  | "MISSING"
  | "UNAVAILABLE";

export function occurrenceDetailState(input: {
  hasData: boolean;
  isPending: boolean;
  isError: boolean;
  errorStatus: number | null;
}): OccurrenceDetailState {
  const readState = backgroundReadState(input);
  if (readState === "READY" || readState === "STALE" || readState === "LOADING") return readState;
  if (input.isError && input.errorStatus === 404) return "MISSING";
  return "UNAVAILABLE";
}
