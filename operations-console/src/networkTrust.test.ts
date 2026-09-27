import { describe, expect, it } from "vitest";
import { isLoopbackHostname, NETWORK_TRUST_WARNING } from "./networkTrust";

describe("network trust warning", () => {
  it.each(["localhost", "LOCALHOST", "127.0.0.1", "::1", "[::1]"])(
    "recognizes loopback hostname %s",
    (hostname) => expect(isLoopbackHostname(hostname)).toBe(true),
  );

  it.each(["0.0.0.0", "192.168.10.20", "workbench.example"])(
    "warns for network-reachable hostname %s",
    (hostname) => expect(isLoopbackHostname(hostname)).toBe(false),
  );

  it("states the actual permission and deployment boundary", () => {
    expect(NETWORK_TRUST_WARNING).toContain("完整权限");
    expect(NETWORK_TRUST_WARNING).toContain("不是安全远程部署");
  });
});
