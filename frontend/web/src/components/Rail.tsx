/**
 * Left rail: resolved context (FR-CTX-6), the attestation card with
 * expandable per-cluster detailed attestation (FR-ATT-8/9), and the
 * check-battery/config-version card (user flow F9's visibility, plus the
 * last rejected config reload per FR-CFG-3).
 */
import { useState } from "react";

import { attestationMarkdown, download } from "../export";
import type { AttestationReport, CheckResult, ContextPayload, ReloadError } from "../types";
import { Evidence } from "./ReportCard";

export function ContextCard(props: { context: ContextPayload | null }) {
  const ctx = props.context;
  return (
    <div className="card">
      <div className="hd">Context</div>
      <div className="bd">
        {!ctx && <div style={{ color: "var(--muted)" }}>Ask a question to resolve context.</div>}
        {ctx && ctx.scope === "cluster" && (
          <>
            <div className="kv"><span className="k">Scope</span><span className="v">cluster</span></div>
            <div className="kv"><span className="k">Cluster</span><span className="v mono">{ctx.clusters[0]}</span></div>
          </>
        )}
        {ctx && ctx.scope === "app" && (
          <>
            <div className="kv"><span className="k">User</span><span className="v">{ctx.user_name || ctx.user_sub}</span></div>
            <div className="kv"><span className="k">Application</span><span className="v mono">{ctx.application}</span></div>
            <div className="kv"><span className="k">Environment</span><span className="v">{ctx.environment}</span></div>
            <div className="kv"><span className="k">Clusters</span><span className="v mono">{ctx.clusters.join(", ")}</span></div>
            <div className="kv">
              <span className="k">Namespaces</span>
              <span className="v mono">{[...new Set(ctx.instances.map((i) => i.namespace))].join(", ")}</span>
            </div>
            {ctx.outside_registered_set && (
              <div style={{ marginTop: 4 }}>
                <span className="pill warn">outside registered set</span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** The full battery for one cluster, passes included (FR-ATT-9). */
function DetailedAttestation(props: { checks: CheckResult[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="att-detail">
      {props.checks.map((check) => (
        <div key={check.id}>
          <div
            className="checkrow"
            style={{ paddingLeft: 4 }}
            onClick={() => setOpen(open === check.id ? null : check.id)}
          >
            <span className="cn">
              {check.name}
              {check.observed ? <span className="obs"> - {check.observed}</span> : null}
            </span>
            <span className={`pill ${check.status}`}>{check.status}</span>
          </div>
          {open === check.id && <Evidence check={check} />}
        </div>
      ))}
    </div>
  );
}

export function AttestationCard(props: { report: AttestationReport | null }) {
  const [openCluster, setOpenCluster] = useState<string | null>(null);
  const report = props.report;
  const age = report ? Math.max(0, Math.round((Date.now() - Date.parse(report.attested_at)) / 1000)) : null;
  return (
    <div className="card">
      <div className="hd">
        Cluster attestation
        {age !== null && <span className="pill dim">{age < 90 ? `${age}s ago` : `${Math.round(age / 60)}m ago`}</span>}
      </div>
      <div className="bd">
        {!report && <div style={{ color: "var(--muted)" }}>Runs before any analysis.</div>}
        {report?.clusters.map((cluster) => (
          <div key={cluster.cluster}>
            <div
              className="attrow"
              onClick={() => setOpenCluster(openCluster === cluster.cluster ? null : cluster.cluster)}
              title="Expand the detailed attestation"
            >
              <div>
                <div className="cl">{openCluster === cluster.cluster ? "▾ " : "▸ "}{cluster.cluster}</div>
                <div className="why">
                  {cluster.verdict === "healthy"
                    ? `all ${cluster.checks.length} checks passed`
                    : cluster.signals.slice(0, 2).join("; ")}
                </div>
              </div>
              <span className={`pill ${cluster.verdict}`}>{cluster.verdict}</span>
            </div>
            {openCluster === cluster.cluster && <DetailedAttestation checks={cluster.checks} />}
          </div>
        ))}
        {report && (
          <div style={{ marginTop: 6 }}>
            <button
              className="pill dim"
              style={{ border: "1px solid var(--line-strong)", cursor: "pointer" }}
              onClick={() => download("attestation.md", attestationMarkdown(report))}
            >
              export .md
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function ChecksCard(props: {
  attestationChecks: number | null;
  app360Checks: number | null;
  configVersion: string;
  reloadError?: ReloadError | null;
}) {
  const err = props.reloadError;
  return (
    <div className="card">
      <div className="hd">
        Checks
        {err && <span className="pill warn">reload rejected</span>}
      </div>
      <div className="bd">
        <div className="kv">
          <span className="k">Attestation battery</span>
          <span className="v">{props.attestationChecks ?? "-"} checks</span>
        </div>
        <div className="kv">
          <span className="k">App 360 battery</span>
          <span className="v">{props.app360Checks ?? "-"} checks</span>
        </div>
        <div className="kv">
          <span className="k">Config version</span>
          <span className="v mono">{props.configVersion || "-"}</span>
        </div>
        {/* FR-CFG-3: the last known good stays live, but the operator has to
            SEE that their edit was refused, and why. */}
        {err && (
          <div className="reload-error" title={err.at}>
            <span className="file mono">{err.file.split("/").slice(-1)[0]}</span>
            <span className="why">{err.message}</span>
            <span className="lkg">Last known good config is still serving conversations.</span>
          </div>
        )}
      </div>
    </div>
  );
}
