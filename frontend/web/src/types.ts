/**
 * Typed card payloads: the TypeScript mirror of backend
 * cloudops/agent/models.py (which is the source of truth). Cards render
 * from these payloads, never from parsed prose (FR-UI-2).
 */

export type CheckStatus =
  | "pass" | "warn" | "fail" | "maintenance" | "unattestable"
  | "error" | "manual" | "registry" | "info";

export interface CheckEvidence {
  tool: string;
  args: Record<string, unknown>;
  timestamp: string;
  triggered_rules: Array<{
    path: string; op: string; value: unknown;
    observed: unknown; outcome: string; reason: string;
  }>;
  runbook?: string | null;
  error?: string | null;
}

export interface CheckResult {
  id: string;
  name: string;
  severity: string;
  status: CheckStatus;
  observed: string;
  reason: string;
  duration_ms: number;
  evidence: CheckEvidence;
}

export interface ClusterAttestation {
  cluster: string;
  verdict: "healthy" | "maintenance" | "degraded" | "unattestable";
  signals: string[];
  checks: CheckResult[];
  battery_version: string;
  attested_at: string;
  duration_ms: number;
}

export interface AttestationReport {
  kind: "attestation";
  clusters: ClusterAttestation[];
  changes: string[];
  attested_at: string;
}

export interface SectionResult {
  section: number;
  title: string;
  source: string;
  status: CheckStatus;
  checks: CheckResult[];
  registry_facts: Record<string, unknown>;
  manual_items: string[];
  /** Analyst-authored, grounded in this section's checks (FR-360-4). */
  findings?: string;
}

export interface App360Report {
  kind: "app360";
  application: string;
  app_label: string;
  cluster: string;
  namespace: string;
  environment: string;
  overall_status: "healthy" | "at_risk" | "critical";
  sections: SectionResult[];
  /** Narrative slots of the organization's template (FR-360-4); empty when
   * the analyst wrote the prose into the transcript only. */
  executive_summary?: string;
  recommendations?: string[];
  final_reason?: string;
  report_date: string;
  battery_version: string;
}

export interface ContextPayload {
  kind: "context";
  scope: "app" | "cluster";
  user_sub: string;
  user_name: string;
  application?: string | null;
  app_label?: string | null;
  environment?: string | null;
  instances: Array<{ cluster: string; namespace: string; environment: string }>;
  clusters: string[];
  outside_registered_set: boolean;
}

export interface ClarifyPayload {
  question: string;
  options: string[];
  kind: string;
}

export interface PhasePayload {
  phase: string;
  status: string;
  at: string;
  [key: string]: unknown;
}

export interface Persona {
  sub: string;
  name: string;
  email: string;
  groups: string[];
}

/** A config edit the agent refused; the last known good stays live (FR-CFG-3). */
export interface ReloadError {
  file: string;
  message: string;
  at: string;
}

/** GET /api/meta: the BFF's own facts plus the agent's config status. */
export interface ConsoleMeta {
  mode: string;
  env: string;
  configVersion: string;
  agentReachable?: boolean;
  batteries?: { attestation: number; app360: number } | null;
  reloadError?: ReloadError | null;
}

/** SSE events from the BFF (server/src/index.ts is the emitter). */
export type StreamEvent =
  | { type: "meta"; contextId: string }
  | { type: "text"; delta: string }
  | { type: "phase"; payload: PhasePayload }
  | { type: "context"; payload: ContextPayload }
  | { type: "clarify"; payload: ClarifyPayload }
  | { type: "attestation"; payload: AttestationReport }
  | { type: "app360"; payload: App360Report }
  | { type: "error"; payload: { correlation_id: string; message: string } }
  | { type: "state"; state: string }
  | { type: "done" };

export interface ChatTurn {
  role: "user" | "agent";
  text: string;
}

export interface LogLine {
  at: string;
  tag: string;
  text: string;
  tone: "dim" | "ok" | "warn" | "crit";
}
