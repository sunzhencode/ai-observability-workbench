/**
 * Turning "what the user did to a secret box" into what the API wants.
 *
 * The API distinguishes three intents -- keep what is stored, replace it, clear
 * it -- because a write-only field cannot be round-tripped: sending an empty
 * string would be ambiguous between "unchanged" and "remove it". That is a
 * protocol requirement.
 *
 * It is not a user interface. Asking someone to pick a verb from a dropdown
 * before typing an address is not how any other system handles a password, and
 * it made a required field look optional. The form now behaves the ordinary
 * way: type to set it, leave it blank to keep what is there, tick a box to
 * remove it. This module maps that back to the protocol.
 */
import type { SecretUpdate } from "./types";

export interface SecretFieldState {
  /** Whether something is already stored for this field. */
  configured: boolean;
  /** What the user typed. Empty means they typed nothing. */
  input: string;
  /** Whether the user asked to remove the stored value. */
  cleared: boolean;
}

export function emptySecretField(configured = false): SecretFieldState {
  return { configured, input: "", cleared: false };
}

/** What to send for a field in this state. */
export function secretUpdateFor(state: SecretFieldState): SecretUpdate {
  if (state.cleared) return { action: "CLEAR" };
  const value = state.input;
  if (value !== "") return { action: "REPLACE", value };
  // Nothing typed: keep a stored value, or send nothing where there is none.
  return state.configured ? { action: "KEEP" } : { action: "CLEAR" };
}

/** The prompt inside the box, which is where "you don't have to retype" belongs. */
export function secretPlaceholder(state: SecretFieldState, hint = ""): string {
  if (state.cleared) return "将被清除";
  if (state.configured) return "已配置 · 留空表示不修改";
  return hint || "保存后不会回显";
}

/**
 * Whether a required field is satisfied.
 *
 * A stored value counts: re-saving a channel must not force the user to retype
 * a credential they cannot read back.
 */
export function secretFieldSatisfied(state: SecretFieldState, required: boolean): boolean {
  if (!required) return true;
  if (state.cleared) return false;
  return state.input !== "" || state.configured;
}
