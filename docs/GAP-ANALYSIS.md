# Gap Analysis: PRD and User Flows vs Implementation

Date: 2026-08-26.
Baseline: develop at f1795fc, after the browser-driven acceptance pass (AC1-AC8 all green).
This document is the plan of record for the bridge iteration; it will be updated as gaps close.

## 1. Verified working (browser plus test evidence)

- F1 zero-question triage: context, dual attestation, two 18-section reports, grounded narrative (AC2).
- F2 clarification: exactly one question, quick-picks, reply folding (AC3).
- F3 onboarding, both halves: guidance without checks, then user-named app with outside-registry caveat (AC4).
- F6 direct attestation, including punctuation and clarify-reply fixes.
- F7 drill-down: per-check evidence trails, detailed attestation (FR-ATT-9), export buttons present.
- F9 happy path: prompt and battery hot-reload with visible config version change (AC5, AC6).
- One distributed trace per turn across all five services with thread.id, via MCP request meta (AC7).
- Canary secret absent from logs (AC7); make check clean: 47 tests, ruff, mypy, tsc x2 (AC8).
- Tool-budget enforcement and hallucinated-tool-name recovery in the analyst loop.

## 2. Gaps

| # | Flow / requirement | Gap | Severity |
|---|---|---|---|
| G1 | F5, FR-ATT-7 | TTL re-attestation runs, but no verdict delta is computed or surfaced; "first, a change" behavior missing and the TTL path is browser-unverified | High |
| G2 | F8, D1 dividend | A narrative-phase failure (inference backend down) collapses into the generic turn error instead of "cards stand, analysis unavailable, correlation id"; per-check error rows browser-unverified | High |
| G3 | F4, FR-ATT-5 | The unattestable verdict (watchdog_absent cluster in the mock scenario) has never been exercised end to end; confidence-capping narrative unverified | High |
| G4 | FR-CTX-7 | Conversational scope override mid-thread ("switch to nonprod", naming a different app) is implemented but untested and unverified | Medium |
| G5 | F7, FR-360-7, FR-ATT-9 | Export markdown content fidelity against the 18-section template is unverified; the spec'd Copy evidence action is missing | Medium |
| G6 | F9, FR-CFG-3 | Config validation errors are logged but not surfaced in the console; the rail should show the last rejected reload | Medium |
| G7 | NFR-QE-1 | No headless mock-mode E2E test and no fake-LLM seam to make one hermetic | Medium |
| G8 | FR-GW-4 | Gateway servers.yaml hot-reload (add or disable a domain without restart) is implemented but has no test and no E2E verification | Medium |
| G9 | NFR-PERF-2, NFR-OBS-4 | First-token under 2 s does not hold on the gpt-oss:20b default (deviation to document with models.yaml guidance); the Jaeger compose path has never been demonstrated | Low |

Out of scope for this iteration (tracked for later milestones): live backends beyond the current guarded interfaces (M3), the evaluation harness (M2, own thread), ITSM and remediation (M4).

## 3. Bridge plan

Fable plans, merges, and browser-verifies; Opus workers implement in dedicated worktrees.

| Package | Branch | Owns | Closes |
|---|---|---|---|
| WP-CORE | wp/core-flows | backend agent tier: attestation delta, narrative-phase degrade, unattestable messaging, scope-override tests | G1 G2 G3 G4 |
| WP-UX | wp/ux-exports | frontend tier: export fidelity, Copy evidence, reload-error surfacing in the rail | G5 G6 |
| WP-QE | wp/qe-e2e | quality tier: fake-LLM seam, headless E2E, gateway hot-reload test, README and deviation notes | G7 G8 G9 |

Merge order: CORE, then QE, then UX, each followed by make check, stack restart, and a browser verification of the affected flows.
Iteration continues (worker follow-ups on their branches) until every gap row is closed or explicitly re-scoped; then develop is cut to UAT.

## 4. Verification script per gap (browser, mock mode)

- G1: set attestation_ttl_seconds low via models.yaml (hot), ask a follow-up after expiry, expect a re-attest tick plus a "changed since last attestation" lead when the scenario differs.
- G2: point models.yaml at a dead inference port (hot), run a triage turn, expect both cards plus an "analysis unavailable" notice carrying a correlation id, and a completed (not failed) turn.
- G3: attest the watchdog_absent scenario cluster, expect an unattestable pill, distinct wording, and a narrative that caps its confidence.
- G4: in a resolved payments-api prod thread, say "switch to nonprod", expect re-resolution and re-attestation of the nonprod instance without a fresh interrogation.
- G5: export both cards and diff the markdown against the 18-section template structure; use Copy evidence on an expanded check.
- G6: save an invalid battery edit, expect the rail to show the rejected reload with the validation reason while conversations continue on last known good.
- G7: run the new E2E test headlessly in make check.
- G8: disable a server in servers.yaml, expect its tools to leave the gateway catalog without restart; re-enable and they return.
