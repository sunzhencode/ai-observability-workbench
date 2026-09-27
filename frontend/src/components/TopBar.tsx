import type { Health } from "../types";
import { ageFrom } from "../util";

interface Props {
  health: Health | null;
  healthError: boolean;
  onRefresh: () => void;
  title: string;
}

export function TopBar({ health, healthError, onRefresh, title }: Props) {
  const connected = !!health && !healthError;
  const pollOk = health?.last_poll_ok;

  let statusText = "connecting…";
  let dotClass = "dot";
  if (healthError) {
    statusText = "backend unreachable";
    dotClass = "dot dot--bad";
  } else if (connected) {
    if (pollOk === false) {
      statusText = "source degraded";
      dotClass = "dot dot--bad";
    } else {
      statusText = health!.last_poll_at
        ? `updated ${ageFrom(health!.last_poll_at)} ago`
        : "waiting for first poll";
      dotClass = "dot dot--ok";
    }
  }

  return (
    <header className="topbar">
      <strong className="topbar__title">{title}</strong>
      <span className="spacer" />
      <span className="status">
        <span className={dotClass} />
        {statusText}
      </span>
      <button className="icon-btn" onClick={onRefresh} aria-label="Refresh now">
        ⟳ Refresh
      </button>
    </header>
  );
}
