/**
 * The link Feishu cards point back to. It belongs with notifications: it is
 * only ever used in a message, and the 系统设置 数据源 tab is data sources
 * only (F22) — 模型服务 is the other tab there, not part of that list.
 */
import { useEffect, useState } from "react";
import { describeRequestFailure } from "../../requestError";
import { useSaveWorkbenchUrl, useWorkbenchUrl } from "../../queries/notifications";

export function WorkbenchUrlPanel() {
  const stored = useWorkbenchUrl();
  const saveUrl = useSaveWorkbenchUrl();
  const [url, setUrl] = useState("");
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const value = stored.data?.url;
    if (value !== undefined && loadedFor === null) {
      setUrl(value);
      setLoadedFor(value);
    }
  }, [stored.data?.url, loadedFor]);

  const save = () => {
    if (saveUrl.isPending) return;
    setMessage("");
    setError("");
    saveUrl
      .mutateAsync(url)
      .then(() => setMessage("工作台链接已保存。"))
      .catch((cause) => {
        const failure = describeRequestFailure(cause, "保存失败。");
        setError(failure.detail ?? failure.title);
      });
  };

  return (
    <section className="editor-section">
      <div className="editor-section__head">
        <div>
          <h3>工作台链接</h3>
          <p>卡片里“打开工作台”用的地址。留空即不附链接——localhost 对群成员不可访问。</p>
        </div>
      </div>
      <div className="workbench-url">
        <input
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://workbench.example"
          value={url}
        />
        <button
          className="secondary-btn"
          disabled={saveUrl.isPending}
          onClick={save}
          type="button"
        >
          保存
        </button>
      </div>
      {message && (
        <div className="toast-line toast-line--ok" role="status">
          {message}
        </div>
      )}
      {(error || stored.isError) && (
        <div className="toast-line toast-line--error" role="alert">
          {error || "工作台链接读取失败。"}
        </div>
      )}
    </section>
  );
}
