/**
 * The Grafana address on a source, and the entry point to importing from it.
 *
 * **Its own little form, deliberately not part of the source form.** The source
 * form saves and takes effect immediately; importing is gated on an explicit
 * successful test. Folding the two together would leave the gate with nothing
 * to hold, because a save would silently be the moment of first use.
 *
 * The gate shows up as exactly one thing: the import button is disabled with a
 * line saying to test first. No draft state, no activation step, none of that
 * vocabulary — those belong to the notification and model pages, which have a
 * real state machine. A data source saves and is in effect.
 */
import { useEffect, useState } from "react";
import { useSaveGrafanaConfig, useTestGrafanaConfig } from "../../queries/grafana";
import { requestFailureText } from "../../requestError";
import {
  emptySecretField,
  secretPlaceholder,
  secretUpdateFor,
  type SecretFieldState,
} from "../../secretField";
import type { EventSource } from "../../types";

interface Props {
  source: EventSource;
  editable: boolean;
  onOpenImport: () => void;
}

export function GrafanaPanel({ source, editable, onOpenImport }: Props) {
  const grafana = source.config?.grafana ?? null;
  const [url, setUrl] = useState(grafana?.url ?? "");
  const [timeout, setTimeoutSeconds] = useState(grafana?.timeout_seconds ?? 15);
  const [secret, setSecret] = useState<SecretFieldState>(
    emptySecretField(grafana?.secret_configured ?? false),
  );
  const [message, setMessage] = useState<string | null>(null);

  // The form follows the selected source: leaving one source's address in the
  // box while another is selected is how a credential ends up saved on the
  // wrong one.
  useEffect(() => {
    setUrl(grafana?.url ?? "");
    setTimeoutSeconds(grafana?.timeout_seconds ?? 15);
    setSecret(emptySecretField(grafana?.secret_configured ?? false));
    setMessage(null);
  }, [source.id, grafana?.url, grafana?.secret_configured, grafana?.timeout_seconds]);

  const save = useSaveGrafanaConfig();
  const test = useTestGrafanaConfig();
  const busy = save.isPending || test.isPending;
  const tested = grafana?.last_test_status === "OK";
  const configured = Boolean(grafana?.url);

  const onSave = async () => {
    setMessage(null);
    try {
      const saved = await save.mutateAsync({
        sourceId: source.id,
        draft: {
          url: url.trim(),
          secret: secretUpdateFor(secret),
          timeout_seconds: timeout,
        },
      });
      // Reset from the server's answer, not from what was typed. Deriving it
      // locally got "there is a stored token but you did not retype it" wrong,
      // and the box then offered to store one that was already there.
      setSecret(
        emptySecretField(saved.config?.grafana?.secret_configured ?? false),
      );
      setMessage(url.trim() === "" ? "已移除 Grafana 配置" : "已保存");
    } catch (error) {
      setMessage(requestFailureText(error, "Grafana 配置未保存；请核对地址和凭证"));
    }
  };

  const onTest = async () => {
    setMessage(null);
    try {
      const result = (await test.mutateAsync(source.id)) as {
        ok: boolean;
        code: string;
      };
      setMessage(result.ok ? "连接成功，可以导入了" : failureText(result.code));
    } catch (error) {
      setMessage(requestFailureText(error, "Grafana 连接测试未完成；配置保持原状态"));
    }
  };

  return (
    <details className="editor-section">
      <summary>
        Grafana（选填）
        <em>{summaryText(configured, tested)}</em>
      </summary>
      <p>
        只用来把 dashboard 读成指标模板，不作为取数通道——指标一律直连 Thanos。
        内网 http 地址可以正常使用。
      </p>
      <div className="form-grid">
        <label className="form-grid__wide">
          <span>Grafana 地址</span>
          <input
            disabled={!editable}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="http://grafana.internal:3000"
            value={url}
          />
        </label>
        <label className="form-grid__wide">
          <span>Service account token（可留空）</span>
          <input
            disabled={!editable}
            onChange={(event) =>
              setSecret({ ...secret, input: event.target.value, cleared: false })
            }
            placeholder={secretPlaceholder(secret, "匿名只读的 Grafana 可以不填")}
            type="password"
            value={secret.input}
          />
        </label>
        {secret.configured && (
          <label className="switch-row">
            <input
              checked={secret.cleared}
              disabled={!editable}
              onChange={(event) =>
                setSecret({ ...secret, cleared: event.target.checked, input: "" })
              }
              type="checkbox"
            />
            <span>清除已保存的 token</span>
          </label>
        )}
        <label>
          <span>超时（秒）</span>
          <input
            disabled={!editable}
            max={120}
            min={1}
            onChange={(event) => setTimeoutSeconds(Number(event.target.value))}
            type="number"
            value={timeout}
          />
        </label>
      </div>
      <div className="inline-actions">
        <button
          className="secondary-btn"
          disabled={!editable || busy}
          onClick={onSave}
          type="button"
        >
          保存 Grafana 配置
        </button>
        <button
          className="secondary-btn"
          disabled={!editable || busy || !configured}
          data-testid="grafana-test"
          onClick={onTest}
          type="button"
        >
          测试连接
        </button>
        <button
          className="primary-btn"
          data-testid="grafana-import-open"
          disabled={!configured || !tested}
          onClick={onOpenImport}
          type="button"
        >
          从 dashboard 导入
        </button>
      </div>
      {configured && !tested && (
        <p className="hint" data-testid="grafana-import-hint">
          先测试连接，成功后才能导入。
        </p>
      )}
      {message !== null && (
        <p className="hint" data-testid="grafana-message" role="status">
          {message}
        </p>
      )}
    </details>
  );
}

function summaryText(configured: boolean, tested: boolean): string {
  if (!configured) return "未配置";
  return tested ? "已连接" : "已填地址，未测试";
}

/**
 * Each failure gets its own next action. "Load failed" would leave the reader
 * with nothing to do, and the 401 case in particular has a specific answer that
 * is not obvious — anonymous read is common enough that needing a token reads
 * as a bug rather than as configuration.
 */
function failureText(code: string): string {
  switch (code) {
    case "GRAFANA_AUTH_REQUIRED":
      return "这个 Grafana 需要认证：请在 Grafana 里建一个 Viewer 权限的 service account token，填在上面。";
    case "GRAFANA_REDIRECT":
      return "Grafana 返回了重定向，本产品不跟随。请确认地址的协议（http / https）与实际服务一致。";
    case "GRAFANA_TIMEOUT":
      return "连接超时。检查地址、端口，以及这台机器能否访问它。";
    case "GRAFANA_UNREACHABLE":
      return "连不上这个地址。检查主机名、端口与网络。";
    case "GRAFANA_NOT_JSON":
      return "返回的不是 Grafana API 的内容，前面可能有一层登录页或网关。";
    case "GRAFANA_RESPONSE_TOO_LARGE":
      return "返回内容超过读取上限，已整体拒绝。";
    default:
      return `连接失败（${code}）。`;
  }
}
