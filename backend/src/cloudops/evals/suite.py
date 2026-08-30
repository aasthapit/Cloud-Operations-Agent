"""The scenario schema: what a suite file is allowed to say.

Suites are DATA. A scenario names a persona, a fleet, a conversation, and
what each turn must produce; it never names a Python symbol, so adding
coverage is a YAML edit and the harness stays the only place that knows how
to boot a stack. The schema is documented for humans at the top of
``backend/evals/suites/context-resolution.yaml``; this module is its
executable definition.

Two shapes are worth calling out.

``Expect.live_only`` holds the expectations that only make sense against a
real model. In fake mode the analyst is ``FakeLlm``, whose narrative is one
deterministic sentence, so asserting narrative content there would pin the
double rather than the product. Everything deterministic - fences, verdicts,
context fields, tool budgets - is asserted in BOTH modes, and that is what
the CI gate runs.

``Fleet`` describes clusters by KIND (healthy, degraded-crashloop,
unreachable, cordoned, pressure) rather than by Kubernetes payload. The kinds
are the vocabulary of the attestation battery's verdicts, so a scenario reads
as a claim about behavior instead of a pile of fixture JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from cloudops.common.config import load_yaml

Mode = Literal["fake", "live"]

CLUSTER_KINDS = ("healthy", "degraded-crashloop", "unreachable", "cordoned", "pressure")

DEFAULT_JUDGE_THRESHOLD = 0.8


class Persona(BaseModel):
    """The identity claims for a case; the same shape dev and OIDC both emit."""

    sub: str
    name: str = ""
    email: str = ""
    groups: list[str] = Field(default_factory=list)


class NamespaceSpec(BaseModel):
    """One application namespace on a cluster."""

    app_label: str
    replicas: int = 2
    # Overrides the cluster kind for this namespace: a healthy cluster can
    # still host a crash-looping application, and vice versa.
    crashloop: bool | None = None


class ClusterSpec(BaseModel):
    kind: Literal["healthy", "degraded-crashloop", "unreachable", "cordoned", "pressure"] = (
        "healthy"
    )
    namespaces: dict[str, NamespaceSpec] = Field(default_factory=dict)


class Fleet(BaseModel):
    """Which clusters exist and what shape they are in.

    ``base: default`` starts from ``cloudops.testkit.default_world`` - the
    canned fleet the pytest suite uses - and ``clusters`` adds to or replaces
    entries in it. ``base: empty`` builds only what the scenario declares.
    """

    base: Literal["default", "empty"] = "default"
    clusters: dict[str, ClusterSpec] = Field(default_factory=dict)


class AppSpec(BaseModel):
    """A registry application row plus its placements.

    Written into the scenario's copy of ``config/fleet/applications.yaml``,
    which is both what the orchestrator reads for ownership and what the
    seeder loads into the scenario's in-memory Mongo. One source, so a
    scenario cannot describe a fleet the registry disagrees with.
    """

    application: str
    app_id: str
    lob: str = "Unassigned"
    app_label: str | None = None
    owner_groups: list[str] = Field(default_factory=list)
    criticality: str = "medium"
    instances: list[dict[str, Any]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    def as_registry_entry(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "app_id": self.app_id,
            "lob": self.lob,
            "app_label": self.app_label or self.application,
            "owner_groups": list(self.owner_groups),
            "criticality": self.criticality,
            "instances": list(self.instances),
            **self.extra,
        }


class Registry(BaseModel):
    """Scenario overrides for the application registry.

    ``base: committed`` keeps config/fleet/applications.yaml and appends (or
    replaces by application name) whatever ``apps`` declares; ``base: empty``
    starts from nothing, which is how an onboarding scenario proves the agent
    behaves when the registry knows the user's team not at all.
    """

    base: Literal["committed", "empty"] = "committed"
    apps: list[AppSpec] = Field(default_factory=list)


class Mention(BaseModel):
    """One narrative-content expectation.

    A bare string in YAML is a case-insensitive substring; a mapping may set
    ``regex: true`` to match a pattern instead.
    """

    pattern: str
    regex: bool = False

    @model_validator(mode="before")
    @classmethod
    def _from_scalar(cls, value: Any) -> Any:
        return {"pattern": value} if isinstance(value, str) else value


class App360Expect(BaseModel):
    emitted: bool | None = None
    count: int | None = None
    overall_status: str | None = None


class PlacementExpect(BaseModel):
    """One resolved instance, as the cluster answered for it (FR-CTX-2)."""

    cluster: str
    namespace: str | None = None
    verified: bool | None = None
    reachable: bool | None = None
    pod_count: int | None = None


class ErrorExpect(BaseModel):
    phase: str | None = None
    reason: str | None = None
    correlation_id: bool = True


class JudgeExpect(BaseModel):
    """Per-metric minimum scores for the LLM judges (live mode only).

    ``enabled: false`` turns the judges off for the turn. It exists for
    scenarios whose NARRATIVE is deliberately broken (the degrade case runs
    against a dead inference endpoint): judging a narrative the scenario
    itself sabotaged measures the sabotage, not the agent.
    """

    enabled: bool = True
    groundedness: float | None = None
    completeness: float | None = None
    protocol_tone: float | None = None

    def thresholds(self, default: float) -> dict[str, float]:
        if not self.enabled:
            return {}
        declared = {
            "groundedness": self.groundedness,
            "completeness": self.completeness,
            "protocol_tone": self.protocol_tone,
        }
        return {k: (default if v is None else v) for k, v in declared.items()}


class LiveOnly(BaseModel):
    """Expectations evaluated only when a real model wrote the narrative."""

    narrative_must_mention: list[Mention] = Field(default_factory=list)
    narrative_must_not_mention: list[Mention] = Field(default_factory=list)
    judge: JudgeExpect = Field(default_factory=JudgeExpect)


class Expect(BaseModel):
    """What one turn must produce."""

    outcome: str | None = None  # resolved | clarify:<kind> | onboarding
    context: dict[str, Any] = Field(default_factory=dict)
    attestation: dict[str, str] = Field(default_factory=dict)
    app360: App360Expect = Field(default_factory=App360Expect)
    phases: list[str] | None = None
    fence_kinds: list[str] | None = None
    placements: list[PlacementExpect] = Field(default_factory=list)
    narrative_must_mention: list[Mention] = Field(default_factory=list)
    narrative_must_not_mention: list[Mention] = Field(default_factory=list)
    max_tool_calls: int | None = None
    clarify_options: int | None = None
    error: ErrorExpect | None = None
    live_only: LiveOnly = Field(default_factory=LiveOnly)


class TurnSpec(BaseModel):
    user: str
    expect: Expect = Field(default_factory=Expect)


class Case(BaseModel):
    id: str
    description: str = ""
    modes: list[Mode] = Field(default_factory=lambda: ["fake", "live"])  # type: ignore[arg-type]
    persona: Persona
    fleet: Fleet = Field(default_factory=Fleet)
    registry: Registry = Field(default_factory=Registry)
    turns: list[TurnSpec]
    judge_threshold: float | None = None
    # Point the analyst at an endpoint that is not there, to exercise the F8
    # narrative degrade. Only meaningful in live mode: the fake model has no
    # endpoint to lose.
    inference_api_base: str | None = None

    def runs_in(self, mode: Mode) -> bool:
        return mode in self.modes


class Suite(BaseModel):
    suite: str
    description: str = ""
    judge_threshold: float = DEFAULT_JUDGE_THRESHOLD
    defaults: dict[str, Any] = Field(default_factory=dict)
    cases: list[Case]

    def threshold_for(self, case: Case) -> float:
        return case.judge_threshold if case.judge_threshold is not None else self.judge_threshold


def load_suite(path: Path) -> Suite:
    """Parse one suite file, applying its ``defaults`` block to every case.

    ``defaults`` is a shallow per-key merge over each case mapping, so a suite
    can declare one fleet and one persona and have its cases say only what is
    different. Sharing a fleet is not just brevity: the harness reuses a booted
    stack across consecutive cases whose world and config plane are identical,
    so a suite with one fleet boots once.
    """
    raw = load_yaml(path) or {}
    defaults = raw.get("defaults") or {}
    cases = [{**defaults, **case} for case in raw.get("cases") or []]
    return Suite.model_validate({**raw, "cases": cases})


def discover_suites(root: Path, names: list[str] | None = None) -> list[Path]:
    """Suite files under ``root``, optionally filtered by stem."""
    paths = sorted(root.glob("*.yaml"))
    if names:
        wanted = set(names)
        paths = [p for p in paths if p.stem in wanted]
        missing = wanted - {p.stem for p in paths}
        if missing:
            raise FileNotFoundError(
                f"no suite named {', '.join(sorted(missing))} under {root}"
            )
    return paths
