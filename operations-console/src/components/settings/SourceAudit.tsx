/**
 * 变更历史 — actions and their results, never addresses or credentials.
 *
 * `PRODUCT_SPEC.md` CAP-09.7: the audit trail must not contain secrets,
 * ciphertext or remote response bodies. The backend enforces that; this only
 * renders what it returns.
 */
import type { EventSourceAudit } from "../../types";

export function SourceAudit({ audit }: { audit: EventSourceAudit[] }) {
  return (
    <details className="editor-section">
      <summary>
        变更历史
        <em>{audit.length > 0 ? `${audit.length} 条` : "暂无"}</em>
      </summary>
      <p>只记录动作与结果，不记录地址凭证或密文。</p>
      <div className="audit-list">
        {audit.slice(0, 8).map((item) => (
          <div className="audit-item" key={item.id}>
            <div className="audit-item__states">
              {item.action} · {item.result}
            </div>
            <div className="audit-item__meta">{new Date(item.created_at).toLocaleString()}</div>
          </div>
        ))}
        {audit.length === 0 && <div className="placeholder-card">暂无变更记录。</div>}
      </div>
    </details>
  );
}
