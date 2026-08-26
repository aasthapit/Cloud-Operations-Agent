"""Typed check and report structures.

These models are triple-duty contracts:
1. The check engine produces them from battery YAML + tool results.
2. Serialized to JSON they ARE the typed card payloads the console renders
   (FR-UI-2, FR-ATT-9, FR-360-9): every report embeds its per-check results
   with full evidence so drill-down never re-queries.
3. Compact projections of them ground the LLM narrative (FR-360-4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# battery definitions (the shape of config/checks/*.yaml)
# ---------------------------------------------------------------------------


class RuleOutcome(StrEnum):
    FAIL = "fail"
    WARN = "warn"
    MAINTENANCE = "maintenance"
    UNATTESTABLE = "unattestable"


class RuleDef(BaseModel):
    path: str
    op: str  # eq|ne|gt|gte|lt|lte|in|not_in|empty|not_empty|truthy|falsy|exists|absent
    value: Any = None
    outcome: RuleOutcome
    reason: str


class CheckDef(BaseModel):
    id: str
    name: str
    severity: str = "warning"  # critical | warning | info
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    observed: str | None = None
    runbook: str | None = None
    rules: list[RuleDef] = Field(default_factory=list)


class AttestationBattery(BaseModel):
    version: int
    battery: str
    scope: str = "cluster"
    defaults: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckDef]


class App360SectionDef(BaseModel):
    section: int
    title: str
    source: str  # checks | registry | narrative | attestation
    checks: list[CheckDef] = Field(default_factory=list)
    registry_fields: list[str] = Field(default_factory=list)
    manual_items: list[str] = Field(default_factory=list)


class App360Battery(BaseModel):
    version: int
    battery: str
    scope: str = "app_instance"
    defaults: dict[str, Any] = Field(default_factory=dict)
    sections: list[App360SectionDef]


# ---------------------------------------------------------------------------
# results (the card payloads)
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    MAINTENANCE = "maintenance"
    UNATTESTABLE = "unattestable"
    ERROR = "error"      # the tool itself failed; counted as unknown, never as pass
    MANUAL = "manual"    # awaiting human input (FR-360-6)
    REGISTRY = "registry"  # sourced from the application registry
    INFO = "info"


class CheckEvidence(BaseModel):
    """The drill-down trail behind one check row (FR-ATT-6, FR-360-9)."""

    tool: str
    args: dict[str, Any]
    timestamp: str
    triggered_rules: list[dict[str, Any]] = Field(default_factory=list)
    runbook: str | None = None
    error: str | None = None


class CheckResult(BaseModel):
    id: str
    name: str
    severity: str
    status: CheckStatus
    observed: str = ""
    reason: str = ""
    duration_ms: float = 0.0
    evidence: CheckEvidence


class ClusterVerdict(StrEnum):
    HEALTHY = "healthy"
    MAINTENANCE = "maintenance"
    DEGRADED = "degraded"
    UNATTESTABLE = "unattestable"


class ClusterAttestation(BaseModel):
    cluster: str
    verdict: ClusterVerdict
    signals: list[str] = Field(default_factory=list)  # short strings for the card row
    checks: list[CheckResult]
    battery_version: str = ""
    attested_at: str = ""
    duration_ms: float = 0.0


class AttestationReport(BaseModel):
    kind: str = "attestation"
    clusters: list[ClusterAttestation]
    changes: list[str] = Field(default_factory=list)  # deltas vs the previous run (F5)
    attested_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class SectionResult(BaseModel):
    section: int
    title: str
    source: str
    status: CheckStatus  # rollup for the section row
    checks: list[CheckResult] = Field(default_factory=list)
    registry_facts: dict[str, Any] = Field(default_factory=dict)
    manual_items: list[str] = Field(default_factory=list)
    findings: str = ""  # LLM-authored, grounded (FR-360-4)


class App360Report(BaseModel):
    kind: str = "app360"
    application: str
    app_label: str
    cluster: str
    namespace: str
    environment: str
    overall_status: str  # healthy | at_risk | critical (FR-360-5)
    sections: list[SectionResult]
    executive_summary: str = ""
    recommendations: list[str] = Field(default_factory=list)
    final_reason: str = ""
    report_date: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    battery_version: str = ""


# ---------------------------------------------------------------------------
# resolved context (the left-rail card payload)
# ---------------------------------------------------------------------------


class AppInstance(BaseModel):
    cluster: str
    namespace: str
    environment: str


class ResolvedContext(BaseModel):
    kind: str = "context"
    scope: str = "app"  # app | cluster
    user_sub: str = ""
    user_name: str = ""
    groups: list[str] = Field(default_factory=list)
    application: str | None = None
    app_label: str | None = None
    environment: str | None = None
    instances: list[AppInstance] = Field(default_factory=list)
    clusters: list[str] = Field(default_factory=list)
    outside_registered_set: bool = False  # user-named app not in their set (F3)
