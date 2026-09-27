export type BackgroundReadState = "LOADING" | "READY" | "STALE" | "UNAVAILABLE";

export function backgroundReadState(input: {
  hasData: boolean;
  isPending: boolean;
  isError: boolean;
}): BackgroundReadState {
  if (input.hasData) return input.isError ? "STALE" : "READY";
  if (input.isPending) return "LOADING";
  if (input.isError) return "UNAVAILABLE";
  return "LOADING";
}
