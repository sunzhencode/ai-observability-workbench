/**
 * One box for a write-only credential.
 *
 * Empty means "leave it alone", typing means "set it to this", and the checkbox
 * -- which only appears when there is something to remove -- means "remove it".
 * The value is never rendered back: `PRODUCT_SPEC.md` CAP-08 forbids writing a
 * saved secret into the DOM, and the browser acceptance run asserts it.
 */
import { secretPlaceholder, type SecretFieldState } from "../secretField";

export function SecretField({
  label,
  help,
  state,
  onChange,
  required = false,
  autoComplete = "new-password",
}: {
  label: string;
  help?: string;
  state: SecretFieldState;
  onChange: (next: SecretFieldState) => void;
  required?: boolean;
  autoComplete?: string;
}) {
  return (
    <label className="secret-field">
      <span>
        {label}
        {required && !state.configured ? "（必填）" : ""}
      </span>
      <input
        autoComplete={autoComplete}
        disabled={state.cleared}
        onChange={(event) => onChange({ ...state, input: event.target.value })}
        placeholder={secretPlaceholder(state)}
        type="password"
        value={state.input}
      />
      {help && <small>{help}</small>}
      {state.configured && (
        <label className="secret-field__clear">
          <input
            checked={state.cleared}
            onChange={(event) =>
              onChange({ ...state, cleared: event.target.checked, input: "" })
            }
            type="checkbox"
          />
          <span>清除已保存的值</span>
        </label>
      )}
    </label>
  );
}
