/**
 * Golden-structure tests for the Markdown exports (G5, FR-360-7, FR-ATT-9).
 *
 * These assert the SHAPE the organization's template demands - 18 sections
 * in order, the executive summary fields, one table per section, findings,
 * numbered recommendations, final assessment last - not exact prose, so
 * wording stays free to improve while the structure stays a contract.
 */
import { describe, expect, it } from "vitest";

import { app360Markdown, attestationMarkdown, evidenceText } from "./export";
import type {
  App360Report,
  AttestationReport,
  CheckResult,
  CheckStatus,
  SectionResult,
} from "./types";

/** The battery's section titles (config/checks/app360.yaml). */
const TITLES = [
  "Executive summary",
  "Application identity",
  "Deployment overview",
  "Runtime health",
  "Configuration",
  "Networking and connectivity",
  "Storage and data",
  "Security posture",
  "Observability",
  "Dependency health",
  "Cluster and platform context",
  "Capacity and performance",
  "Release and change history",
  "Reliability and recovery",
  "Operational risks and gaps",
  "Supportability and ownership",
  "Recommendations",
  "Final assessment",
];

function check(over: Partial<CheckResult> = {}): CheckResult {
  return {
    id: "workload-availability",
    name: "Workload availability",
    severity: "critical",
    status: "fail" as CheckStatus,
    observed: "2/3 ready",
    reason: "ready replicas below desired",
    duration_ms: 12,
    evidence: {
      tool: "ocp__get_workloads",
      args: { cluster: "prod-east-1", namespace: "payments-prod" },
      timestamp: "2026-08-26T09:15:04Z",
      triggered_rules: [
        {
          path: "replicas_mismatch", op: "not_empty", value: null,
          observed: ["payments-api"], outcome: "fail", reason: "ready replicas below desired",
        },
      ],
      runbook: "https://runbooks.example.internal/payments-api",
      error: null,
    },
    ...over,
  };
}

function section(n: number, over: Partial<SectionResult> = {}): SectionResult {
  return {
    section: n,
    title: TITLES[n - 1],
    source: "checks",
    status: "pass" as CheckStatus,
    checks: [],
    registry_facts: {},
    manual_items: [],
    ...over,
  };
}

function report(over: Partial<App360Report> = {}): App360Report {
  const sections = TITLES.map((_, i) => section(i + 1));
  sections[1] = section(2, {
    source: "registry",
    status: "registry" as CheckStatus,
    registry_facts: {
      "owners.business": "payments product team",
      "owners.technical": "payments-eng",
      "owners.support_team": "payments-eng on-call",
      sla: "99.9% monthly",
    },
  });
  sections[2] = section(3, {
    status: "fail" as CheckStatus,
    checks: [check()],
    findings: "One replica has been unready since the 09:02 rollout.",
  });
  sections[4] = section(5, { manual_items: ["Review application parameters"] });
  return {
    kind: "app360",
    application: "payments-api",
    app_label: "payments-api",
    cluster: "prod-east-1",
    namespace: "payments-prod",
    environment: "prod",
    overall_status: "at_risk",
    sections,
    executive_summary: "payments-api is at risk on prod-east-1.",
    recommendations: ["Restore the third replica", "Re-check memory headroom"],
    final_reason: "One deterministic critical check is failing.",
    report_date: "2026-08-26T09:15:00Z",
    battery_version: "a1b2c3d",
    ...over,
  };
}

function headings(markdown: string): Array<[number, string]> {
  return [...markdown.matchAll(/^## (\d+)\. (.+)$/gm)].map((m) => [Number(m[1]), m[2]]);
}

describe("app360Markdown", () => {
  it("renders all 18 template sections in order, final assessment last", () => {
    const found = headings(app360Markdown(report()));
    expect(found).toHaveLength(18);
    expect(found.map(([n]) => n)).toEqual([...Array(18).keys()].map((i) => i + 1));
    expect(found[found.length - 1]).toEqual([18, "Final assessment"]);
  });

  it("orders sections numerically even when the payload arrives shuffled", () => {
    const shuffled = report();
    shuffled.sections = [...shuffled.sections].reverse();
    expect(headings(app360Markdown(shuffled)).map(([n]) => n)).toEqual(
      [...Array(18).keys()].map((i) => i + 1),
    );
  });

  it("carries every executive summary field of the template", () => {
    const summary = app360Markdown(report()).split("## 2.")[0];
    for (const field of [
      "Application Name", "Namespace", "Cluster", "Environment", "Business Owner",
      "Technical Owner", "Support Team", "Report Date", "Overall Status", "Summary",
    ]) {
      expect(summary).toContain(`- ${field}: `);
    }
    expect(summary).toContain("- Business Owner: payments product team");
    expect(summary).toContain("- Overall Status: At Risk");
  });

  it("renders one Item | Value table per populated section, with observed values and statuses", () => {
    const md = app360Markdown(report());
    const deployment = md.split("## 3.")[1].split("## 4.")[0];
    expect(deployment).toContain("| Item | Value |");
    expect(deployment).toContain("| Workload availability | 2/3 ready - FAIL (ready replicas below desired) |");
    expect(deployment.match(/\| Item \| Value \|/g)).toHaveLength(1);
  });

  it("keeps registry and manual rows visible rather than dropping them (FR-360-6)", () => {
    const md = app360Markdown(report());
    // Registry paths render under the template's own item names.
    expect(md).toContain("| Support Team | payments-eng on-call - REGISTRY |");
    expect(md).toContain("| SLA | 99.9% monthly - REGISTRY |");
    expect(md).toContain("| Review application parameters | awaiting input - MANUAL |");
  });

  it("spells out registry paths the template does not name", () => {
    const custom = report();
    custom.sections[15] = {
      ...custom.sections[15],
      registry_facts: { "vendor.support_tier": "gold" },
    };
    expect(app360Markdown(custom)).toContain("| Vendor support tier | gold - REGISTRY |");
  });

  it("prints the section findings note when the analyst wrote one", () => {
    const deployment = app360Markdown(report()).split("## 3.")[1].split("## 4.")[0];
    expect(deployment).toContain("**Findings**");
    expect(deployment).toContain("- One replica has been unready since the 09:02 rollout.");
  });

  it("numbers the recommendations and closes with status, reason, next review", () => {
    const md = app360Markdown(report());
    const recommendations = md.split("## 17.")[1].split("## 18.")[0];
    expect(recommendations).toContain("1. Restore the third replica");
    expect(recommendations).toContain("2. Re-check memory headroom");
    const final = md.split("## 18.")[1];
    expect(final).toContain("- Status: At Risk");
    expect(final).toContain("- Reason: One deterministic critical check is failing.");
    expect(final).toContain("- Next Review Date: not set");
  });

  it("marks empty narrative slots instead of inventing prose", () => {
    const md = app360Markdown(
      report({ executive_summary: "", recommendations: [], final_reason: "" }),
    );
    expect(md).toContain("- Summary: not authored this turn");
    expect(md).toContain("- Reason: not authored this turn");
    expect(md.split("## 17.")[1].split("## 18.")[0]).toContain("not authored this turn");
  });
});

const attestation: AttestationReport = {
  kind: "attestation",
  attested_at: "2026-08-26T09:14:40Z",
  changes: ["prod-east-2: healthy -> degraded"],
  clusters: [
    {
      cluster: "prod-east-1",
      verdict: "healthy",
      signals: [],
      battery_version: "3",
      attested_at: "2026-08-26T09:14:40Z",
      duration_ms: 210,
      checks: [check({ id: "nodes-ready", name: "Nodes ready", status: "pass", observed: "6/6 ready", reason: "" })],
    },
    {
      cluster: "prod-east-2",
      verdict: "degraded",
      signals: ["1 node NotReady"],
      battery_version: "3",
      attested_at: "2026-08-26T09:14:41Z",
      duration_ms: 245,
      checks: [
        check({ id: "nodes-ready", name: "Nodes ready", status: "pass", observed: "6/6 ready", reason: "" }),
        check({ id: "etcd-health", name: "etcd health", status: "fail", observed: "1 member unhealthy", reason: "etcd member down" }),
      ],
    },
  ],
};

describe("attestationMarkdown", () => {
  it("opens with a verdict summary over every cluster", () => {
    const md = attestationMarkdown(attestation);
    expect(md).toContain("## Verdict summary");
    expect(md).toContain("| Cluster | Verdict |");
    expect(md).toContain("| prod-east-1 | HEALTHY - 1/1 checks passed |");
    expect(md).toContain("| prod-east-2 | DEGRADED - 1/2 checks passed |");
    expect(md).toContain("- Verdicts: 1 healthy, 1 degraded");
  });

  it("renders the battery per cluster with observed values and statuses", () => {
    const block = attestationMarkdown(attestation).split("## prod-east-2")[1];
    expect(block).toContain("- Signals: 1 node NotReady");
    expect(block).toContain("| Nodes ready | 6/6 ready - PASS |");
    expect(block).toContain("| etcd health | 1 member unhealthy - FAIL (etcd member down) |");
  });

  it("prints evidence for non-passing checks only", () => {
    const block = attestationMarkdown(attestation).split("## prod-east-2")[1];
    expect(block).toContain("**Evidence**");
    expect(block).toContain("- check etcd-health - etcd health");
    expect(block).not.toContain("- check nodes-ready");
    expect(attestationMarkdown(attestation).split("## prod-east-2")[0]).not.toContain("**Evidence**");
  });

  it("closes with the changes since the previous attestation", () => {
    expect(attestationMarkdown(attestation)).toContain("## Changes since previous attestation");
    expect(attestationMarkdown(attestation)).toContain("- prod-east-2: healthy -> degraded");
  });
});

describe("evidenceText", () => {
  it("is a compact block: check, tool, args, observed vs rule, outcome, timestamp, runbook", () => {
    const lines = evidenceText(check()).split("\n");
    expect(lines[0]).toBe("check workload-availability - Workload availability");
    expect(lines).toContain("tool: ocp__get_workloads");
    expect(lines).toContain('args: {"cluster":"prod-east-1","namespace":"payments-prod"}');
    expect(lines).toContain("observed: 2/3 ready");
    // value-less ops read badly as "vs not_empty null"
    expect(lines).toContain(
      'rule replicas_mismatch: observed ["payments-api"] (rule: not_empty) -> fail: ready replicas below desired',
    );
    expect(lines).toContain("outcome: fail - ready replicas below desired");
    expect(lines).toContain("timestamp: 2026-08-26T09:15:04Z");
    expect(lines).toContain("runbook: https://runbooks.example.internal/payments-api");
  });
});
