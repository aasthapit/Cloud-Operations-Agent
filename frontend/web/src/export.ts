/**
 * Markdown export of report payloads (FR-360-7, FR-ATT-9).
 *
 * The Application 360 export reproduces the organization's report template
 * (18 sections, transcribed in docs/reference/source-checklists.md; export
 * skeleton in config/agent/templates/app360_report.md): an executive
 * summary block, one `Item | Value` table per section, per-section
 * findings, numbered recommendations, and the final assessment last.
 *
 * Every row is populated from the typed report object, never from parsed
 * prose. Narrative slots the analyst left empty say so instead of being
 * dropped or invented, the same rule that keeps manual and registry rows
 * visible (FR-360-6).
 */
import type {
  App360Report,
  AttestationReport,
  CheckResult,
  ClusterAttestation,
  SectionResult,
} from "./types";

/** Narrative lives in the transcript when the analyst wrote no field. */
const NOT_AUTHORED = "not authored this turn; see the conversation transcript";

const OVERALL_STATUS_LABEL: Record<string, string> = {
  healthy: "Healthy",
  at_risk: "At Risk",
  critical: "Critical",
};

/** Registry paths whose last segment is an acronym in the template's labels. */
const ACRONYMS = new Set(["sla", "slo", "rpo", "rto", "dr", "id", "url"]);

/**
 * Registry paths (config/checks/app360.yaml `registry_fields`) under the
 * item names the template uses; anything unmapped falls back to a spelled
 * out path so a new field still reads sensibly.
 */
const FIELD_LABELS: Record<string, string> = {
  application: "Application Name",
  criticality: "Business Criticality",
  "owners.business": "Business Owner",
  "owners.technical": "Technical Owner",
  "owners.support_team": "Support Team",
  on_call: "Primary On-Call Team",
  escalation: "Escalation Path",
  runbook_url: "Runbook",
  monitoring_owner: "Monitoring Owner",
  docs_status: "Documentation Status",
  dependencies: "Dependencies",
  "backup.policy": "Backup Policy",
  "backup.rpo": "RPO Target",
  "backup.rto": "RTO Target",
};

/** Table cells are single-line: pipes and newlines would break the row. */
function cell(value: unknown): string {
  const text = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "");
  return text.replace(/\|/g, "\\|").replace(/\s*\n\s*/g, " ").trim();
}

function statusLabel(status: string): string {
  return status.toUpperCase();
}

/** `owners.support_team` -> `Support Team`; an unmapped `foo.bar` -> `Foo bar`. */
function factLabel(path: string): string {
  const mapped = FIELD_LABELS[path];
  if (mapped) return mapped;
  const words = path.split(".").flatMap((seg) => seg.split("_"));
  const spelled = words.map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w));
  const [first, ...rest] = spelled;
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(" ");
}

/** Template row shape: `<observed> - <status>`, with the reason where there is one. */
function checkValue(check: CheckResult): string {
  const observed = cell(check.observed) || "-";
  const reason = check.reason ? ` (${cell(check.reason)})` : "";
  return `${observed} - ${statusLabel(check.status)}${reason}`;
}

/** One `Item | Value` table per section: checks, registry facts, manual items. */
function sectionRows(section: SectionResult): Array<[string, string]> {
  return [
    ...section.checks.map((c): [string, string] => [cell(c.name), checkValue(c)]),
    ...Object.entries(section.registry_facts).map(([key, value]): [string, string] => [
      factLabel(key),
      `${cell(value)} - REGISTRY`,
    ]),
    ...section.manual_items.map((item): [string, string] => [cell(item), "awaiting input - MANUAL"]),
  ];
}

function table(rows: Array<[string, string]>, headers: string[] = ["Item", "Value"]): string[] {
  if (rows.length === 0) return [];
  return [
    `| ${headers.join(" | ")} |`,
    `|${headers.map(() => "---").join("|")}|`,
    ...rows.map((row) => `| ${row.join(" | ")} |`),
    "",
  ];
}

/** Registry facts are attached to their own section; the summary block needs a few by path. */
function registryFact(report: App360Report, path: string): string {
  for (const section of report.sections) {
    if (path in section.registry_facts) return cell(section.registry_facts[path]);
  }
  return "not in registry";
}

function findingsBlock(section: SectionResult): string[] {
  if (!section.findings) return [];
  return ["**Findings**", "", `- ${section.findings.replace(/\s*\n\s*/g, " ").trim()}`, ""];
}

export function app360Markdown(report: App360Report): string {
  const sections = [...report.sections].sort((a, b) => a.section - b.section);
  const lines: string[] = ["# OpenShift Application 360 Report", ""];

  for (const section of sections) {
    lines.push(`## ${section.section}. ${section.title}`, "");

    if (section.section === 1) {
      lines.push(
        `- Application Name: ${report.application}`,
        `- Namespace: ${report.namespace}`,
        `- Cluster: ${report.cluster}`,
        `- Environment: ${report.environment}`,
        `- Business Owner: ${registryFact(report, "owners.business")}`,
        `- Technical Owner: ${registryFact(report, "owners.technical")}`,
        `- Support Team: ${registryFact(report, "owners.support_team")}`,
        `- Report Date: ${report.report_date}`,
        `- Overall Status: ${OVERALL_STATUS_LABEL[report.overall_status] ?? report.overall_status}`,
        `- Summary: ${report.executive_summary || NOT_AUTHORED}`,
        "",
      );
    }

    if (section.section === 17) {
      const recommendations = report.recommendations ?? [];
      lines.push(
        ...(recommendations.length > 0
          ? recommendations.map((rec, i) => `${i + 1}. ${rec}`)
          : [`_${NOT_AUTHORED}_`]),
        "",
      );
    }

    if (section.section === 18) {
      // No review cadence reaches the MVP's registry; the field stays
      // visible and unset rather than being invented (FR-360-6).
      const nextReview = registryFact(report, "next_review_date");
      lines.push(
        `- Status: ${OVERALL_STATUS_LABEL[report.overall_status] ?? report.overall_status}`,
        `- Reason: ${report.final_reason || NOT_AUTHORED}`,
        `- Next Review Date: ${nextReview === "not in registry" ? "not set" : nextReview}`,
        "",
      );
    }

    lines.push(...table(sectionRows(section)), ...findingsBlock(section));
  }

  if (report.battery_version) {
    lines.push(
      "---",
      "",
      `_Application 360 battery version ${report.battery_version}. Table rows are deterministic check, registry, and manual facts; summary, findings, recommendations, and reason are analyst-authored and grounded in those rows._`,
      "",
    );
  }
  return lines.join("\n");
}

/**
 * The evidence trail behind one check as plain text: the same lines the
 * console's Copy evidence action puts on the clipboard and the attestation
 * export prints under non-passing checks (FR-ATT-6, FR-360-9).
 */
export function evidenceLines(check: CheckResult): string[] {
  const ev = check.evidence;
  const lines = [
    `check ${check.id} - ${check.name}`,
    `tool: ${ev.tool}`,
    `args: ${JSON.stringify(ev.args)}`,
  ];
  if (check.observed) lines.push(`observed: ${check.observed}`);
  for (const rule of ev.triggered_rules) {
    // value-less ops (not_empty, truthy...) read badly as "vs op null"
    const expectation = rule.value == null ? `(rule: ${rule.op})` : `vs ${rule.op} ${JSON.stringify(rule.value)}`;
    lines.push(
      `rule ${rule.path}: observed ${JSON.stringify(rule.observed)} ${expectation}` +
        ` -> ${rule.outcome}: ${rule.reason}`,
    );
  }
  lines.push(`outcome: ${check.status}${check.reason ? ` - ${check.reason}` : ""}`);
  if (ev.error) lines.push(`error: ${ev.error}`);
  lines.push(`timestamp: ${ev.timestamp}`);
  if (ev.runbook) lines.push(`runbook: ${ev.runbook}`);
  return lines;
}

export function evidenceText(check: CheckResult): string {
  return evidenceLines(check).join("\n");
}

function clusterBlock(cluster: ClusterAttestation): string[] {
  const failing = cluster.checks.filter((c) => c.status !== "pass");
  const lines: string[] = [
    `## ${cluster.cluster} - ${statusLabel(cluster.verdict)}`,
    "",
    `- Battery version: ${cluster.battery_version || "-"}`,
    `- Attested: ${cluster.attested_at || "-"}`,
    `- Signals: ${cluster.signals.length > 0 ? cluster.signals.join("; ") : "none"}`,
    "",
    ...table(
      cluster.checks.map((c): [string, string] => [cell(c.name), checkValue(c)]),
      ["Check", "Result"],
    ),
  ];
  if (failing.length > 0) {
    lines.push("**Evidence**", "");
    for (const check of failing) {
      const [head, ...rest] = evidenceLines(check);
      lines.push(`- ${head}`, ...rest.map((line) => `  - ${line}`));
    }
    lines.push("");
  }
  return lines;
}

export function attestationMarkdown(report: AttestationReport): string {
  const counts = report.clusters.reduce<Record<string, number>>((acc, c) => {
    acc[c.verdict] = (acc[c.verdict] ?? 0) + 1;
    return acc;
  }, {});
  const lines: string[] = [
    "# Cluster Health Attestation",
    "",
    `- Attested: ${report.attested_at}`,
    `- Clusters: ${report.clusters.length}`,
    `- Verdicts: ${Object.entries(counts).map(([v, n]) => `${n} ${v}`).join(", ") || "none"}`,
    "",
    "## Verdict summary",
    "",
    ...table(
      report.clusters.map((c): [string, string] => [
        cell(c.cluster),
        `${statusLabel(c.verdict)} - ${c.checks.filter((x) => x.status === "pass").length}/${c.checks.length} checks passed`,
      ]),
      ["Cluster", "Verdict"],
    ),
  ];
  for (const cluster of report.clusters) lines.push(...clusterBlock(cluster));
  lines.push("## Changes since previous attestation", "");
  lines.push(...(report.changes.length > 0 ? report.changes.map((c) => `- ${c}`) : ["- none"]), "");
  return lines.join("\n");
}

export function download(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
