/**
 * Markdown export of report payloads (FR-360-7, FR-ATT-9).
 * Follows config/agent/templates/app360_report.md's skeleton; narrative
 * fields live in the chat transcript, so exports carry the deterministic
 * facts and mark narrative slots for the analyst text.
 */
import type { App360Report, AttestationReport, CheckResult } from "./types";

function checkLine(check: CheckResult): string {
  const observed = check.observed ? ` - ${check.observed}` : "";
  const reason = check.reason ? ` (${check.reason})` : "";
  return `| ${check.name} | ${check.status.toUpperCase()}${observed}${reason} |`;
}

export function app360Markdown(report: App360Report): string {
  const lines: string[] = [
    "# OpenShift Application 360 Report",
    "",
    "## 1. Executive Summary",
    `- Application Name: ${report.application}`,
    `- Namespace: ${report.namespace}`,
    `- Cluster: ${report.cluster}`,
    `- Environment: ${report.environment}`,
    `- Report Date: ${report.report_date}`,
    `- Overall Status: ${report.overall_status.replace("_", " ")}`,
    "- Summary: see the analyst narrative in the conversation transcript",
    "",
  ];
  for (const section of report.sections) {
    if (section.section === 1) continue;
    lines.push(`## ${section.section}. ${section.title}`, "");
    if (section.checks.length > 0) {
      lines.push("| Item | Value |", "|---|---|");
      for (const check of section.checks) lines.push(checkLine(check));
      lines.push("");
    }
    const facts = Object.entries(section.registry_facts);
    if (facts.length > 0) {
      lines.push("| Registry fact | Value |", "|---|---|");
      for (const [k, v] of facts) lines.push(`| ${k} | ${typeof v === "object" ? JSON.stringify(v) : String(v)} |`);
      lines.push("");
    }
    for (const item of section.manual_items) lines.push(`- [manual] ${item}`);
    if (section.manual_items.length > 0) lines.push("");
  }
  return lines.join("\n");
}

export function attestationMarkdown(report: AttestationReport): string {
  const lines: string[] = ["# Cluster Health Attestation", `- Attested: ${report.attested_at}`, ""];
  for (const cluster of report.clusters) {
    lines.push(`## ${cluster.cluster} - ${cluster.verdict.toUpperCase()}`, "");
    lines.push("| Check | Result |", "|---|---|");
    for (const check of cluster.checks) lines.push(checkLine(check));
    lines.push("");
  }
  if (report.changes.length > 0) {
    lines.push("## Changes since previous attestation", "");
    for (const change of report.changes) lines.push(`- ${change}`);
  }
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
