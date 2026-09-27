import { describe, expect, it } from "vitest";
import {
  CHANNEL_KINDS,
  KIND_LABELS,
  canSaveChannel,
  channelTargetSummary,
  describeRequirements,
  emptyConfig,
  parseRecipients,
  portForTlsMode,
  tlsModeForPort,
} from "./channelDraft";
import type { NotificationChannel } from "./types";

describe("kinds", () => {
  it("every kind has a label and a hint that says where to get the credential", () => {
    for (const kind of CHANNEL_KINDS) {
      expect(KIND_LABELS[kind]).toBeTruthy();
      expect(describeRequirements(emptyConfig(kind)).length).toBeGreaterThan(0);
    }
  });

  it("each kind starts from its own shape", () => {
    expect(emptyConfig("SMTP").kind).toBe("SMTP");
    expect(emptyConfig("GENERIC_WEBHOOK").kind).toBe("GENERIC_WEBHOOK");
    expect(emptyConfig("FEISHU_CUSTOM_BOT").kind).toBe("FEISHU_CUSTOM_BOT");
  });

  it("tells the user the token is part of the feishu webhook, not a field", () => {
    // This is the exact question the page could not answer before F24.
    const lines = describeRequirements(emptyConfig("FEISHU_CUSTOM_BOT")).join(" ");
    expect(lines).toContain("token");
    expect(lines).toContain("最后那一段");
  });

  it("warns that a generic webhook receives the alert content", () => {
    const lines = describeRequirements(emptyConfig("GENERIC_WEBHOOK")).join(" ");
    expect(lines).toContain("169.254.169.254");
    expect(lines).toContain("https://");
  });
});

describe("smtp port and tls stay in step", () => {
  it("pairs 587 with STARTTLS and 465 with implicit TLS", () => {
    expect(portForTlsMode("STARTTLS")).toBe(587);
    expect(portForTlsMode("TLS")).toBe(465);
    expect(tlsModeForPort(587)).toBe("STARTTLS");
    expect(tlsModeForPort(465)).toBe("TLS");
  });
});

describe("canSaveChannel", () => {
  it("needs a name whatever the kind", () => {
    for (const kind of CHANNEL_KINDS) {
      expect(canSaveChannel("  ", emptyConfig(kind))).toBe(false);
    }
  });

  it("needs a recipient and a sender for smtp", () => {
    const base = emptyConfig("SMTP");
    if (base.kind !== "SMTP") throw new Error("wrong kind");
    expect(canSaveChannel("mail", { ...base, host: "smtp.example.com" })).toBe(false);
    expect(
      canSaveChannel("mail", {
        ...base,
        host: "smtp.example.com",
        from_addr: "bot@example.com",
        to_addrs: ["ops@example.com"],
      }),
    ).toBe(true);
  });

  it("refuses a webhook that is not https before the request is made", () => {
    const base = emptyConfig("GENERIC_WEBHOOK");
    if (base.kind !== "GENERIC_WEBHOOK") throw new Error("wrong kind");
    expect(canSaveChannel("hook", { ...base, url: "http://example.com/x" })).toBe(false);
    expect(canSaveChannel("hook", { ...base, url: "https://example.com/x" })).toBe(true);
  });

  it("does not judge secrets, so a saved channel stays savable untouched", () => {
    // The credential sits behind an empty box meaning "leave it alone". Reading
    // the draft value here made an already-saved channel impossible to re-save.
    expect(canSaveChannel("ops", emptyConfig("FEISHU_CUSTOM_BOT"))).toBe(true);
  });
});

describe("parseRecipients", () => {
  it("accepts commas, semicolons and newlines, and de-duplicates", () => {
    expect(parseRecipients("a@x.com, b@x.com\nc@x.com; a@x.com")).toEqual([
      "a@x.com",
      "b@x.com",
      "c@x.com",
    ]);
  });

  it("ignores blank entries", () => {
    expect(parseRecipients(" \n,\n ")).toEqual([]);
  });
});

describe("channelTargetSummary", () => {
  const channel = (summary: unknown): NotificationChannel =>
    ({
      id: 1,
      name: "c",
      state: "ENABLED",
      active_revision_id: null,
      revisions: [{ version: 1, config_summary: summary }],
    }) as unknown as NotificationChannel;

  it("shows what a channel points at", () => {
    expect(
      channelTargetSummary(channel({ target: "smtp.example.com:587", detail: "2 个收件人" })),
    ).toBe("smtp.example.com:587 · 2 个收件人");
  });

  it("survives a channel with no revision yet", () => {
    const empty = { revisions: [] } as unknown as NotificationChannel;
    expect(channelTargetSummary(empty)).toBe("尚未配置");
  });
});
