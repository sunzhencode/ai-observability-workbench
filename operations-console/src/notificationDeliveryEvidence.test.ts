import { describe, expect, it } from "vitest";
import { describeDeliveryEvidence } from "./notificationDeliveryEvidence";

describe("notification delivery evidence", () => {
  it("projects the bounded evidence and deep links from the immutable payload", () => {
    expect(
      describeDeliveryEvidence({
        operational_occurrence_id: 42,
        evidence_status: "AVAILABLE",
        metric_evidence: [
          {
            metric_id: "checkout_http_error_ratio",
            display_name: "Checkout error ratio",
            latest: 0.08,
            minimum: 0.02,
            maximum: 0.08,
            unit: "ratio",
          },
        ],
        deep_links: [
          { label: "Checkout API / Errors", url: "https://grafana.example/d/checkout" },
        ],
      }),
    ).toEqual({
      occurrenceId: 42,
      status: "AVAILABLE",
      safeCode: null,
      metrics: [
        {
          id: "checkout_http_error_ratio",
          label: "Checkout error ratio",
          summary: "最新 0.08 · 范围 0.02–0.08 ratio",
        },
      ],
      links: [
        { label: "Checkout API / Errors", url: "https://grafana.example/d/checkout" },
      ],
    });
  });

  it("makes a failed optional projection explicit without exposing internals", () => {
    expect(
      describeDeliveryEvidence({
        evidence_status: "UNAVAILABLE",
        evidence_safe_code: "NOTIFICATION_EVIDENCE_PROJECTION_FAILED",
      }),
    ).toMatchObject({
      status: "UNAVAILABLE",
      safeCode: "NOTIFICATION_EVIDENCE_PROJECTION_FAILED",
      metrics: [],
      links: [],
    });
  });
});
