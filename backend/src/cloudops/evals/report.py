"""The two outputs: one for a machine, one for a person.

``scorecard.json`` is the record: every suite, case, turn and metric with its
verdict and detail, plus the run's mode and judge setting. It is what a CI job
diffs and what a regression is argued from.

``report.md`` is the read: a summary table, a line per case, and - for every
failed metric - the offending narrative excerpt and the evidence it should
have been grounded in. A failure the reader has to go re-run to understand is
a failure that gets ignored.

Markdown convention here matches the repo's: one sentence per line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cloudops.evals.scorers import Metric


@dataclass
class TurnResult:
    index: int
    user: str
    outcome: str
    narrative: str
    duration_s: float
    tool_calls: list[str] = field(default_factory=list)
    analyst_tool_calls: list[str] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(m.passed for m in self.metrics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn": self.index,
            "user": self.user,
            "outcome": self.outcome,
            "pass": self.passed,
            "duration_s": self.duration_s,
            "tool_calls": self.tool_calls,
            "analyst_tool_calls": self.analyst_tool_calls,
            "narrative_excerpt": _excerpt(self.narrative),
            "metrics": [m.as_dict() for m in self.metrics],
        }


@dataclass
class CaseResult:
    id: str
    description: str
    skipped: str = ""
    error: str = ""
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if self.skipped:
            return True
        return not self.error and all(t.passed for t in self.turns)

    def failures(self) -> list[tuple[int, Metric]]:
        return [(t.index, m) for t in self.turns for m in t.metrics if not m.passed]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case": self.id, "description": self.description, "pass": self.passed,
            "turns": [t.as_dict() for t in self.turns],
        }
        if self.skipped:
            payload["skipped"] = self.skipped
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class SuiteResult:
    suite: str
    description: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite, "description": self.description, "pass": self.passed,
            "cases": [c.as_dict() for c in self.cases],
        }


@dataclass
class Scorecard:
    mode: str
    judge: str
    suites: list[SuiteResult] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def cases(self) -> list[CaseResult]:
        return [c for s in self.suites for c in s.cases]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.suites)

    def totals(self) -> dict[str, int]:
        cases = self.cases
        metrics = [m for c in cases for t in c.turns for m in t.metrics]
        return {
            "suites": len(self.suites),
            "cases": len(cases),
            "cases_passed": sum(1 for c in cases if c.passed and not c.skipped),
            "cases_failed": sum(1 for c in cases if not c.passed),
            "cases_skipped": sum(1 for c in cases if c.skipped),
            "turns": sum(len(c.turns) for c in cases),
            "metrics": len(metrics),
            "metrics_failed": sum(1 for m in metrics if not m.passed),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "mode": self.mode,
            "judge": self.judge,
            "pass": self.passed,
            "totals": self.totals(),
            "suites": [s.as_dict() for s in self.suites],
        }


def _excerpt(text: str, limit: int = 600) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + " ..."


def write_outputs(scorecard: Scorecard, out_dir: Path) -> tuple[Path, Path]:
    """Write scorecard.json and report.md; return both paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scorecard_path = out_dir / "scorecard.json"
    scorecard_path.write_text(
        json.dumps(scorecard.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    report_path = out_dir / "report.md"
    report_path.write_text(render_report(scorecard), encoding="utf-8")
    return scorecard_path, report_path


def render_report(scorecard: Scorecard) -> str:
    totals = scorecard.totals()
    lines: list[str] = [
        "# Cloud Operations Agent - evaluation report",
        "",
        f"Generated {scorecard.generated_at} in `{scorecard.mode}` mode "
        f"with judges `{scorecard.judge}`.",
        f"{totals['cases_passed']} of {totals['cases']} cases passed "
        f"({totals['cases_skipped']} skipped) across {totals['metrics']} metrics.",
        "",
        "| Suite | Cases | Passed | Failed | Skipped |",
        "|---|---:|---:|---:|---:|",
    ]
    for suite in scorecard.suites:
        cases = suite.cases
        lines.append(
            f"| {suite.suite} | {len(cases)} | "
            f"{sum(1 for c in cases if c.passed and not c.skipped)} | "
            f"{sum(1 for c in cases if not c.passed)} | "
            f"{sum(1 for c in cases if c.skipped)} |"
        )
    lines.append("")

    for suite in scorecard.suites:
        lines += [f"## {suite.suite}", ""]
        if suite.description:
            lines += [suite.description, ""]
        for case in suite.cases:
            mark = "skip" if case.skipped else ("pass" if case.passed else "FAIL")
            lines.append(f"### [{mark}] {case.id}")
            lines.append("")
            if case.description:
                lines += [case.description, ""]
            if case.skipped:
                lines += [f"Skipped: {case.skipped}.", ""]
                continue
            if case.error:
                lines += ["The case did not complete:", "", "```", case.error, "```", ""]
                continue
            for turn in case.turns:
                verdict = "pass" if turn.passed else "FAIL"
                lines.append(
                    f"- turn {turn.index} [{verdict}] `{turn.user}` "
                    f"-> {turn.outcome}, {len(turn.metrics)} metric(s), {turn.duration_s}s"
                )
            lines.append("")
            failures = case.failures()
            if not failures:
                continue
            lines += ["Failed metrics:", ""]
            for index, metric in failures:
                lines.append(f"- turn {index} `{metric.metric}`: {metric.detail}")
                if metric.score is not None:
                    lines.append(f"  - score {metric.score:.2f} against threshold "
                                 f"{metric.threshold}")
                narrative = metric.evidence.get("narrative")
                if narrative:
                    lines.append(f"  - narrative: {narrative}")
                reason = metric.evidence.get("reason")
                if reason:
                    lines.append(f"  - judge: {reason}")
                for claim in metric.evidence.get("claims", []) or []:
                    lines.append(f"  - claim: {json.dumps(claim, ensure_ascii=False)}")
            lines.append("")
    return "\n".join(lines) + "\n"
