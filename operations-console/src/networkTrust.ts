export const NETWORK_TRUST_WARNING =
  "网络可达者拥有读取证据、修改配置、改变 Incident 状态及触发通知或模型调用的完整权限；当前不是安全远程部署。";

export function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1";
}
