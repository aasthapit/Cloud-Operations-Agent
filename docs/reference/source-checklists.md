# Source operations checklists (transcription and coverage)

Transcribed from photos of the internal operations document supplied on 2026-08-25 (pages 2, 4-9, and 11-15 of a 60-page document were captured).
This file is the traceability bridge between that document and the executable check batteries in `config/checks/`.
Verify against the source document before treating any single item as authoritative; photo transcription can drop or garble a word.

Coverage tags used below describe how the MVP accounts for each item:

| Tag | Meaning |
|---|---|
| `auto` | Deterministic MCP-tool check in the MVP batteries (mock and live contracts) |
| `reg` | Sourced from the application registry (`config/fleet/applications.yaml`) |
| `llm` | LLM narrative, grounded in check results |
| `manual` | Rendered in the report as a manual field awaiting human input |
| `M3` | Deferred to phase M3 (live-mode probes: connectivity, log stores, drift diffs) |
| `M4` | Deferred to phase M4 (ITSM, scanners, feature-flag and backup systems) |

Nothing is silently dropped: every item below appears in the Application 360 report, tagged with its source, per FR-360-6.

## Part A: Detailed Operations Checklist (15 categories, 161 items)

### 1. Application Identity and Ownership

- Confirm application name `auto`
- Confirm namespace `auto`
- Confirm cluster name and cluster ID `auto`
- Confirm environment: Dev / QA / UAT / Prod `auto`
- Identify business owner `reg`
- Identify technical owner `reg`
- Identify support/on-call team `reg`
- Confirm criticality level `reg`
- Confirm SLA/SLO targets `reg`
- Confirm last report date and last review date `auto`

### 2. Deployment Review

- Identify workload type: Deployment / StatefulSet / DaemonSet / Job / CronJob `auto`
- Confirm desired replica count `auto`
- Confirm current replica count `auto`
- Confirm ready replica count `auto`
- Confirm updated replica count `auto`
- Validate rollout status `auto`
- Review deployment strategy `auto`
- Review image repository `auto`
- Review image tag or digest `auto`
- Check last deployment timestamp `auto`
- Check rollback history `auto`
- Check for failed revisions `auto`

### 3. Pod and Runtime Health

- List all pods in the application namespace `auto`
- Confirm pod phase for each pod `auto`
- Check for Pending pods `auto`
- Check for CrashLoopBackOff pods `auto`
- Check for ImagePullBackOff pods `auto`
- Check for OOMKilled events `auto`
- Review restart counts `auto`
- Check readiness probe status `auto`
- Check liveness probe status `auto`
- Check startup probe status if used `auto`
- Review CPU usage `auto`
- Review memory usage `auto`
- Review ephemeral storage usage `M3`
- Review pod age `auto`
- Review node placement and scheduling distribution `auto`

### 4. Configuration Review

- List ConfigMaps used by the application `auto` (names and metadata only)
- List Secrets used by the application `auto` (names and metadata only, never values)
- Verify environment variables `auto` (source references only, values redacted)
- Review mounted volumes `auto`
- Review application parameters `manual`
- Review feature flags `M4`
- Check for config drift across environments `M3`
- Check for missing or invalid config values `manual`
- Validate secret rotation status `M3`

### 5. Networking and Access

- Identify Service objects `auto`
- Identify Route or Ingress objects `auto`
- Confirm service type `auto`
- Confirm external hostnames `auto`
- Confirm TLS termination mode `auto`
- Validate DNS resolution `M3`
- Validate internal service connectivity `M3`
- Validate external endpoint connectivity `M3`
- Check NetworkPolicies affecting the app `auto` (presence and selectors; effect analysis later)
- Check firewall or egress restrictions `manual`
- Confirm exposed ports `auto`
- Confirm upstream/downstream endpoints `reg`

### 6. Storage and Data Protection

- List PVCs attached to the application `auto`
- Check PVC binding status `auto`
- Check PV status `auto`
- Check storage class `auto`
- Review mount status `auto`
- Review capacity usage `auto`
- Review storage growth trend `auto`
- Check for disk pressure or near-full volumes `auto`
- Confirm backup coverage `reg`
- Confirm restore test evidence `reg`
- Confirm DR/replication strategy if applicable `reg`

### 7. Security Review

- Confirm ServiceAccount used by the app `auto`
- Review Role and RoleBinding permissions `auto` (listing; privilege analysis later)
- Review ClusterRole and ClusterRoleBinding if applicable `M3`
- Check SCC or PSA compliance `auto`
- Review pod security context `auto`
- Review secret usage and exposure risk `manual`
- Review image vulnerability status `M4`
- Review TLS certificate validity and expiry `auto`
- Review network exposure level `auto`
- Confirm least-privilege access posture `manual`
- Check for policy violations or security alerts `M4`

### 8. Observability and Monitoring

- Confirm log collection is working `auto`
- Confirm metrics are visible `auto` (Watchdog and staleness checks)
- Confirm dashboards exist `auto` (Grafana links; embedded panels in M3)
- Review current alerts `auto`
- Review recent alert history `auto`
- Review application error rate `auto`
- Review latency or response time `auto`
- Review throughput or transaction volume `auto`
- Review traces/APM if available `M4`
- Review log error patterns `M4`
- Confirm alert ownership and escalation path `reg`

### 9. Dependency Health

- Identify upstream dependencies `reg`
- Identify downstream dependencies `reg`
- Review database connectivity `M3`
- Review cache connectivity `M3`
- Review message queue health `M3`
- Review third-party API health `M3`
- Review timeout and retry patterns `manual`
- Review circuit breaker status if used `manual`
- Review dependency SLAs `reg`
- Confirm failure impact on the application `llm`

### 10. Platform and Cluster Context

- Check node health `auto`
- Check node pressure conditions `auto`
- Check namespace quota status `auto`
- Check LimitRange constraints `auto`
- Check OpenShift operator health `auto`
- Check cluster version `auto`
- Check ingress/router health `auto`
- Check platform alerts `auto`
- Check cluster incidents affecting the namespace `M4`
- Check scheduling capacity in the cluster `auto`

### 11. Capacity and Performance

- Compare requests vs limits `auto`
- Compare actual usage vs requests `auto`
- Check CPU throttling `auto`
- Check memory pressure `auto`
- Review HPA status `auto`
- Review VPA if used `auto`
- Review scaling behavior `auto`
- Review peak usage periods `auto`
- Review growth trend `auto`
- Assess capacity headroom `auto`

### 12. Release and Change History

- Review last release date `auto`
- Review current build version `auto`
- Review image digest `auto`
- Review change tickets `M4`
- Review deployment approvals `M4`
- Review failed rollout history `auto`
- Review rollback events `auto`
- Review feature flag changes `M4`
- Review release cadence `auto`
- Review pending changes `M4`

### 13. Reliability and Recovery

- Confirm backup policy `reg`
- Confirm backup execution status `M4`
- Confirm restore validation `reg`
- Confirm failover strategy `reg`
- Confirm RPO target `reg`
- Confirm RTO target `reg`
- Confirm DR readiness `reg`
- Identify single points of failure `llm`
- Confirm HA design `auto` (replica count and topology spread signals)
- Review recovery runbook `reg`

### 14. Operational Risks and Gaps

- List known incidents `M4`
- List recurring failures `M4` (restart-pattern signals surface earlier, in category 3)
- List expiring certificates or tokens `auto`
- List deprecated versions `M3` (API deprecation signals)
- List unsupported components `manual`
- List manual operational steps `manual`
- List missing documentation `manual`
- List open technical debt items `manual`
- Rank top risks by severity `llm`
- Assign owner and target date `manual`

### 15. Supportability and Readiness

- Confirm runbooks are available `reg`
- Confirm support contacts are current `reg`
- Confirm escalation matrix exists `reg`
- Confirm monitoring owner `reg`
- Confirm application owner `reg`
- Confirm infrastructure owner `reg`
- Confirm vendor support status `reg`
- Confirm documentation completeness `manual`
- Confirm operational handover readiness `manual`
- Confirm next review date `auto`

### Coverage rollup

| Tag | Items | Share |
|---|---|---|
| `auto` (deterministic checks, MVP) | 92 | 57% |
| `reg` (application registry) | 27 | 17% |
| `llm` (grounded narrative) | 3 | 2% |
| `manual` (human field in report) | 14 | 9% |
| `M3` (live-mode probes) | 12 | 7% |
| `M4` (ITSM / scanners / external systems) | 13 | 8% |
| Total | 161 | 100% |

## Part B: OpenShift Application 360 Report Template (18 sections)

The captured pages also contain a prompt ("Provide the list of items to consider in my APP VIEW report") whose 15 areas mirror the checklist categories above, and the report template itself.
The template's field lists as captured:

1. **Executive Summary**: Application Name, Namespace, Cluster (name/ID), Environment (Dev/QA/UAT/Prod), Business Owner, Technical Owner, Support Team, Report Date, Overall Status (Healthy / At Risk / Critical), Summary (1-3 sentences on current application posture).
2. **Application Identity** (Item | Value): Application Name, Namespace, Cluster, Environment, Business Criticality (Low / Medium / High / Mission Critical), SLA/SLO, Version/Release, Deployment Frequency, Last Deployment Time, Change Ticket / Release ID.
3. **Deployment Overview**: Workload Type, Desired / Current / Ready / Updated Replicas, Rollout Status (Healthy / In Progress / Failed), Deployment Strategy (Rolling / Recreate / Blue-Green / Canary), Image Repository, Image Tag / Digest, Deployment Age, plus Deployment Notes.
4. **Runtime Health**: Pod Status (Running / Pending / CrashLoopBackOff / ImagePullBackOff / OOMKilled), Number of Pods, Restart Count, Readiness Probe (Pass / Fail / N/A), Liveness Probe, CPU Usage, Memory Usage, Ephemeral Storage Usage, Node Placement, plus Runtime Findings.
5. **Configuration**: ConfigMaps Used, Secrets Used, Environment Variables, Mounted Volumes, Feature Flags, External Config Sources, plus Configuration Notes.
6. **Networking and Connectivity**: Service Name(s), Service Type (ClusterIP / NodePort / LoadBalancer / Headless), Route / Ingress Hostname, TLS Termination (Edge / Passthrough / Re-encrypt / None), NetworkPolicies Affecting App (Yes / No), Internal Dependencies, External Endpoints, DNS / Connectivity Status (Healthy / Degraded / Failed), plus Networking Findings.
7. **Storage and Data**: PVCs, PVs, Storage Class, Mount Status (Healthy / Degraded / Failed), Capacity Usage, Growth Trend (Stable / Increasing / Critical), Backup Coverage (Yes / No), Restore Test Status (Pass / Fail / N/A), plus Storage Notes.
8. **Security Posture**: ServiceAccount, Roles / RoleBindings, SCC / PSA Compliance (Compliant / Non-Compliant), Secret Handling (Healthy / Needs Review), Image Vulnerabilities (None / Low / Medium / High), TLS Certificate Status (Valid / Expiring / Expired), Exposure Level (Internal / External / Restricted), plus Security Findings.
9. **Observability**: Logging Available (Yes / No), Monitoring Dashboard (Link / None), Alerts Active (Yes / No), Recent Incidents, Tracing / APM (Yes / No), SLO Status (Met / At Risk / Breached), Error Rate, Latency / Response Time, plus Observability Notes.
10. **Dependency Health** (Dependency Type | Status | Notes): Database, Cache, Message Queue, External API, Downstream Services, Upstream Services, each rated Healthy / Degraded / Down, plus Dependency Notes.
11. **Cluster and Platform Context**: Node Health (Healthy / Degraded / Unhealthy), Scheduling Pressure (None / Moderate / High), Namespace Quota Status (OK / Near Limit / Breached), LimitRange Impact (None / Yes), Cluster Version, Cluster Operator Health, OpenShift Ingress / Router (Healthy / Degraded / Unhealthy), plus Platform Notes.
12. **Capacity and Performance**: Resource Requests, Resource Limits, Actual CPU Usage, and further fields cut off in the captured photo (the page break fell mid-table).
13. **Release and Change History**: fields not captured (page missing from photos); presumed to mirror checklist category 12.
14. **Reliability and Recovery**: fields not captured (page missing); presumed to mirror checklist category 13.
15. **Operational Risks**: fields not captured (page missing); presumed to mirror checklist category 14.
16. **Supportability and Ownership**: Primary On-Call Team, Escalation Path, Runbook Available (Yes / No), Monitoring Owner, Documentation Status (Complete / Partial / Missing), Vendor Involvement (Yes / No).
17. **Recommendations**: numbered list, 1 to 3.
18. **Final Assessment**: Status (Healthy / Attention Needed / Critical), Reason (1-3 sentence conclusion), Next Review Date (YYYY-MM-DD).

### Transcription gaps and normalization decisions

1. Template sections 13-15 field lists and the tail of section 12 were not in the captured pages; share those pages to complete the transcription (the check battery for them is currently derived from checklist categories 12-14).
2. The source uses "At Risk" in section 1 but "Attention Needed" in section 18 for the middle status; the product standardizes on Healthy / At Risk / Critical everywhere (FR-360-5).
3. The source also offers per-section "Findings" free-text blocks; these map to the LLM-authored narrative fields, grounded per FR-360-4.
