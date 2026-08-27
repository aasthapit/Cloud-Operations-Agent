/**
 * The Application 360 report card (FR-UI-1/2/7, FR-360-6/7/9): 18 sections
 * as expandable rows, each check opening to its embedded evidence trail;
 * manual and registry rows render labeled instead of disappearing.
 *
 * The Evidence trail is exported: the rail's detailed attestation renders
 * the same component, so one drill-down surface serves both cards.
 */
import { useEffect, useState } from "react";

import { app360Markdown, download, evidenceText } from "../export";
import type { App360Report, CheckResult } from "../types";

export function Evidence(props: { check: CheckResult }) {
  const ev = props.check.evidence;
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(evidenceText(props.check));
      setCopied(true);
    } catch {
      // No clipboard outside a secure context; the trail is on screen
      // anyway, so stay quiet rather than alarming the operator.
    }
  };

  return (
    <div className="evidence">
      <span className="hl">
        check {props.check.id} - tool {ev.tool} - {ev.timestamp.slice(11, 19)}
      </span>
      <span>args: {JSON.stringify(ev.args)}</span>
      {props.check.observed && <span>observed: {props.check.observed}</span>}
      {ev.triggered_rules.map((rule, i) => (
        <span key={i}>
          {/* value-less ops (not_empty, is_true...) read badly as "vs op null" */}
          observed {rule.path} = {JSON.stringify(rule.observed)}{" "}
          {rule.value == null ? `(rule: ${rule.op})` : `vs ${rule.op} ${JSON.stringify(rule.value)}`}{" "}
          - {rule.outcome}: {rule.reason}
        </span>
      ))}
      {ev.error && <span>error: {ev.error}</span>}
      {ev.runbook && (
        <span>
          runbook: <a href={ev.runbook} target="_blank" rel="noreferrer">{ev.runbook.split("/").slice(-1)[0]}</a>
        </span>
      )}
      <button
        className="copy-evidence"
        onClick={copy}
        title="Copy this evidence trail as plain text"
      >
        {copied ? "copied" : "copy evidence"}
      </button>
    </div>
  );
}

export function ReportCard(props: { report: App360Report }) {
  const { report } = props;
  const [openSection, setOpenSection] = useState<number | null>(
    // The most interesting failing section starts open.
    report.sections.find((s) => s.checks.some((c) => c.status === "fail"))?.section ?? null,
  );
  const [openCheck, setOpenCheck] = useState<string | null>(null);

  return (
    <div className="report">
      <div className="rh">
        <span>
          Application 360 - {report.application}{" "}
          <span className="sub">{report.namespace} @ {report.cluster}</span>
        </span>
        <span className={`pill ${report.overall_status}`}>{report.overall_status.replace("_", " ")}</span>
      </div>
      {report.sections.map((section) => {
        const isOpen = openSection === section.section;
        const summaryCheck = section.checks.find((c) => c.status === "fail") ?? section.checks.find((c) => c.status === "warn");
        return (
          <div key={section.section}>
            <div
              className="sec"
              onClick={() => setOpenSection(isOpen ? null : section.section)}
            >
              <span className="sn">
                {isOpen ? "▾ " : "▸ "}
                {section.section}. {section.title}
                {summaryCheck?.observed ? ` - ${summaryCheck.observed}` : ""}
              </span>
              <span className={`pill ${section.status}`}>{section.status}</span>
            </div>
            {isOpen && (
              <>
                {section.checks.map((check) => (
                  <div key={check.id}>
                    <div
                      className="checkrow"
                      onClick={() => setOpenCheck(openCheck === check.id ? null : check.id)}
                    >
                      <span className="cn">
                        {check.name}
                        {check.observed ? <span className="obs"> - {check.observed}</span> : null}
                      </span>
                      <span className={`pill ${check.status}`}>{check.status}</span>
                    </div>
                    {openCheck === check.id && <Evidence check={check} />}
                  </div>
                ))}
                {Object.entries(section.registry_facts).map(([key, value]) => (
                  <div className="checkrow" key={key}>
                    <span className="cn">
                      {key} <span className="obs"> - {typeof value === "object" ? JSON.stringify(value) : String(value)}</span>
                    </span>
                    <span className="pill registry">registry</span>
                  </div>
                ))}
                {section.manual_items.map((item) => (
                  <div className="checkrow" key={item}>
                    <span className="cn">{item}</span>
                    <span className="pill manual">manual</span>
                  </div>
                ))}
              </>
            )}
          </div>
        );
      })}
      <div className="exports">
        <button onClick={() => download(`app360-${report.application}-${report.cluster}.md`, app360Markdown(report))}>
          Export .md
        </button>
      </div>
    </div>
  );
}
