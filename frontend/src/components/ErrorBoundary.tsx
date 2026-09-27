/**
 * Keeps one broken page from turning the whole workbench white.
 *
 * A deep link can now put any page into a state nobody reached by clicking, so
 * "renders nothing at all" stopped being a theoretical failure — F23 R7.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The user gets the message below; the console keeps the stack, which is
    // the only place a local developer can go looking.
    console.error("页面渲染失败", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <main className="management-page">
        <div className="config-error" role="alert">
          <strong>这个页面没能渲染出来。</strong>
          <p>{error.message || "没有更多信息。"}</p>
          <button className="secondary-btn" onClick={() => window.location.reload()} type="button">
            重新加载
          </button>
        </div>
      </main>
    );
  }
}
