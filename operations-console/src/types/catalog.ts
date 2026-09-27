import type { MatcherOperator } from "./core";
import type { IncidentTimelineEntry } from "./operations";

export type ServiceCriticality = "TIER_0" | "TIER_1" | "TIER_2" | "TIER_3";
export type ServiceStatus = "ACTIVE" | "ARCHIVED";
export type ServiceAssignmentState =
  | "UNMAPPED"
  | "MAPPED"
  | "SERVICE_AMBIGUOUS"
  | "SERVICE_ARCHIVED";

export interface ServiceDraft {
  name: string;
  slug: string;
  criticality: ServiceCriticality;
  links: string[];
}

export interface Service extends ServiceDraft {
  id: number;
  ack_sla_seconds: number;
  status: ServiceStatus;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ServiceMappingRuleDraft {
  name: string;
  priority: number;
  service_id: number;
  enabled: boolean;
  source_ids: string[];
  matchers: { label: string; operator: MatcherOperator; value: string }[];
}

export interface ServiceMappingRule extends ServiceMappingRuleDraft {
  id: number;
  service_name: string;
  version: number;
  published_version: number;
  published_at: string | null;
  has_unpublished_changes: boolean;
  created_at: string;
  updated_at: string;
}

export interface ServiceMappingPreview {
  rule_id: number;
  matched_alert_count: number;
  mapped_occurrence_count: number;
  ambiguous_occurrence_count: number;
  unmapped_occurrence_count: number;
  samples: {
    occurrence_id: number;
    title: string;
    assignment_state: Exclude<ServiceAssignmentState, "SERVICE_ARCHIVED">;
    service_ids: number[];
  }[];
}

export interface ServiceAssignmentResult {
  occurrence_id: number;
  service_id: number;
  service_name: string;
  assignment_origin: "MANUAL";
  service_assignment_state: "MAPPED";
  version: number;
  timeline: IncidentTimelineEntry;
  replayed: boolean;
}
