import { useEffect, useState } from "react";
import {
  useSourceNoiseControls,
  useUpdateSourceNoiseControls,
} from "../../queries/noise";

export function NoiseControlsPanel({
  sourceId,
  editable,
}: {
  sourceId: string;
  editable: boolean;
}) {
  const controls = useSourceNoiseControls(sourceId);
  const update = useUpdateSourceNoiseControls(sourceId);
  const [flappingEnabled, setFlappingEnabled] = useState(true);
  const [stormEnabled, setStormEnabled] = useState(true);
  const [alertThreshold, setAlertThreshold] = useState(100);
  const [occurrenceThreshold, setOccurrenceThreshold] = useState(20);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!controls.data) return;
    setFlappingEnabled(controls.data.flapping_enabled);
    setStormEnabled(controls.data.storm_enabled);
    setAlertThreshold(controls.data.storm_alert_threshold);
    setOccurrenceThreshold(controls.data.storm_occurrence_threshold);
    setNotice("");
  }, [controls.data?.version, sourceId]);

  if (controls.isPending) {
    return <section className="config-section"><p>正在读取该来源的通知降噪设置…</p></section>;
  }
  if (controls.isError || !controls.data) {
    return <section className="config-section"><div className="error">降噪设置暂时无法读取；来源采集配置不受影响。</div></section>;
  }
  const valid = alertThreshold >= 10 && alertThreshold <= 10_000
    && occurrenceThreshold >= 5 && occurrenceThreshold <= 1_000;
  const dirty = flappingEnabled !== controls.data.flapping_enabled
    || stormEnabled !== controls.data.storm_enabled
    || alertThreshold !== controls.data.storm_alert_threshold
    || occurrenceThreshold !== controls.data.storm_occurrence_threshold;

  return (
    <section className="config-section noise-controls-panel">
      <div className="config-section__head">
        <div>
          <span className="eyebrow">Noise controls</span>
          <h3>平台通知降噪</h3>
          <p>只合并本工作台发送的协作消息；不会隐藏事件、删除原始告警，也不会创建 Alertmanager Silence。</p>
        </div>
        {controls.data.storm_active ? (
          <span className="pill pill--warning">告警风暴生效中</span>
        ) : null}
      </div>
      <div className="noise-control-grid">
        <label className="noise-control-card">
          <span><strong>合并反复触发</strong><small>同一告警 15 分钟内至少 4 次触发/恢复转换后合并通知；稳定 30 分钟自动清除。</small></span>
          <input
            checked={flappingEnabled}
            disabled={!editable}
            onChange={(event) => setFlappingEnabled(event.target.checked)}
            type="checkbox"
          />
        </label>
        <div className="noise-control-card noise-control-card--storm">
          <label>
            <span><strong>合并告警风暴</strong><small>按最近 5 分钟统计；事件仍逐条进入队列，只把同策略和通知目标的消息汇总。</small></span>
            <input
              checked={stormEnabled}
              disabled={!editable}
              onChange={(event) => setStormEnabled(event.target.checked)}
              type="checkbox"
            />
          </label>
          <div className="noise-thresholds">
            <label><span>新告警达到</span><input disabled={!editable || !stormEnabled} min={10} max={10000} onChange={(event) => setAlertThreshold(Number(event.target.value))} type="number" value={alertThreshold} /></label>
            <label><span>或新事件达到</span><input disabled={!editable || !stormEnabled} min={5} max={1000} onChange={(event) => setOccurrenceThreshold(Number(event.target.value))} type="number" value={occurrenceThreshold} /></label>
          </div>
          <small>连续两个窗口都低于阈值的一半后自动退出风暴状态。</small>
        </div>
      </div>
      {!valid ? <small className="error">新告警阈值需为 10–10,000，新事件阈值需为 5–1,000。</small> : null}
      <div className="form-actions">
        <button
          className="secondary-btn"
          disabled={!editable || !dirty || !valid || update.isPending}
          onClick={() => update.mutate({
            value: {
              flapping_enabled: flappingEnabled,
              storm_enabled: stormEnabled,
              storm_alert_threshold: alertThreshold,
              storm_occurrence_threshold: occurrenceThreshold,
            },
            expectedVersion: controls.data.version,
          }, { onSuccess: () => setNotice("平台通知降噪设置已保存。") })}
          type="button"
        >保存通知降噪设置</button>
      </div>
      {notice ? <small role="status">{notice}</small> : null}
      {update.error instanceof Error ? <small className="error" role="alert">设置未保存：{update.error.message}</small> : null}
    </section>
  );
}
